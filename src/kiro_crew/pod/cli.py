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
import os
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


# Default health-wait budget for `pod up`, in seconds. A first pod boot does
# real extra work beyond serving /api/health -- config migration, staging CLI
# files, minting a fresh .local_secret -- and on a loaded host a healthy
# gateway was measured needing well past the old 45s wall, so the default is
# twice that. Override per-run with `pod up --wait-secs` or the env var below.
POD_HEALTH_WAIT_SECS_DEFAULT = 90
POD_HEALTH_WAIT_SECS_ENV = "KIROCREW_POD_HEALTH_SECS"
# Floor so a misconfigured tiny value cannot make every boot fail before the
# gateway has any chance to answer; ceiling so an absurd value cannot wedge
# `pod up` for hours or overflow float arithmetic on the deadline.
_POD_HEALTH_WAIT_FLOOR_SECS = 5
_POD_HEALTH_WAIT_CEIL_SECS = 3600
#: :func:`_wait_healthy` verdict: the pod was still serving when the budget expired,
#: having published no internal-API credential. Distinct from every other verdict
#: because it is the only one describing a LIVE gateway, and because it is the one
#: verdict no HTTP status can express. Two causes reach it -- a credential write that
#: failed, and one that simply landed after the budget -- so the message names both
#: the journal and the budget escalation and lets the pod's own log separate them.
#: Negative for the same reason as :data:`rt.HEALTH_FOREIGN`: the caller's success
#: test is membership in ``(200, 401, 403)``, so every non-answer must fall outside
#: it. Kept here rather than in ``rt`` because it is this wait's verdict, not a
#: property of a pod.
#:
#: Derived from ``rt.HEALTH_FOREIGN`` rather than written as a literal, and pinned
#: distinct by a test: the two sentinels share one return channel, and -2 was
#: already taken, so a literal here silently routed this verdict into the
#: foreign-port branch and told the operator to pick a different port for a
#: credential problem.
HEALTH_NO_CREDENTIAL = rt.HEALTH_FOREIGN - 1
#: The budget expired while the gateway WAS serving on the final poll, but it had
#: not been serving long enough for the credential verdict to be honest about it.
#:
#: Carried as its own verdict rather than collapsed into the bare status, because
#: "it answered, just not for long" is knowledge only this wait has, and folding it
#: into ``last_http`` destroys it: that is 0 whenever no error status was seen, which
#: is indistinguishable from a port nothing ever answered. The caller then re-derived
#: liveness from the unit -- and ``restarts == 0`` is false for every pod that ever
#: auto-restarted, exactly the recovered-pod population this wait exists to serve --
#: so a gateway last seen answering 200 was reported as never healthy, with the one
#: remedy that applies (a bigger budget) suppressed.
#:
#: Derived from :data:`HEALTH_NO_CREDENTIAL` for the reason given above it, and pinned
#: distinct from every sibling by a test.
HEALTH_SERVING_TOO_BRIEFLY = HEALTH_NO_CREDENTIAL - 1
#: How long a pod must have been CONTIGUOUSLY serving before the exhausted budget
#: is attributed to its credential write rather than to a slow boot. One poll
#: interval, which is the smallest span that can carry the claim: the verdict says
#: the gateway was watched serving, a full poll was waited out, and no credential
#: appeared. A gateway that binds in the final second is serving on the last poll
#: while having had no chance to publish, and its remedy is a bigger budget.
_MIN_SERVING_SPAN_SECS = 1.0


def _health_wait_secs(args: argparse.Namespace) -> int:
    """Resolve the `pod up` health-wait budget in seconds: flag > env > default.

    The env var must parse as a positive integer; a malformed value warns on
    stderr and falls back to the default rather than raising, so a typo in a
    profile cannot make `pod up` unbootable. The result is clamped to a small
    floor for the same reason in the other direction.
    """
    secs = getattr(args, "wait_secs", None)
    if secs is not None:
        if secs <= 0:
            # The warning promises the default, so the default is what applies:
            # an invalid explicit flag does not fall through to the env var.
            print(
                f"pod: ignoring --wait-secs {secs} (not a positive integer); "
                f"using the default {POD_HEALTH_WAIT_SECS_DEFAULT}s",
                file=sys.stderr,
            )
            secs = POD_HEALTH_WAIT_SECS_DEFAULT
    else:
        raw = os.environ.get(POD_HEALTH_WAIT_SECS_ENV, "").strip()
        if raw:
            try:
                secs = int(raw)
                if secs <= 0:
                    raise ValueError(raw)
            except ValueError:
                print(
                    f"pod: ignoring {POD_HEALTH_WAIT_SECS_ENV}={raw!r} (not a "
                    f"positive integer); using the default "
                    f"{POD_HEALTH_WAIT_SECS_DEFAULT}s",
                    file=sys.stderr,
                )
                secs = POD_HEALTH_WAIT_SECS_DEFAULT
    if secs is None:
        secs = POD_HEALTH_WAIT_SECS_DEFAULT
    return min(max(secs, _POD_HEALTH_WAIT_FLOOR_SECS), _POD_HEALTH_WAIT_CEIL_SECS)


def _wait_healthy(
    cfg: PodConfig,
    name: str,
    port: int,
    tries: int = POD_HEALTH_WAIT_SECS_DEFAULT,
    superseded: str = "",
) -> int:
    """Poll until the pod is USABLE (serving 200/401/403 *and* credentialled), or
    bail fast on failure.

    *superseded* is the credential that was on disk BEFORE this command started the
    gateway, and readiness then requires a credential that is not that one. A pod
    home survives a crash -- ``clear_marker`` runs only on a graceful shutdown and
    the stale-marker prune deliberately never removes the credential -- so without
    this a reboot of a crashed pod reads its predecessor's secret on the first poll,
    calls the pod ready before the new gateway has published anything, and hands the
    mint a credential that gateway never minted. Left ``""`` when there is nothing
    to supersede: a pod that was ALREADY active is not a new generation, so its
    current credential is the right one and must not be waited out.

    Returns the HTTP code on success, or a negative sentinel on early failure:
      -1 = the unit's gateway crashed / is crash-looping (a broken worktree build
           — the thing under test won't boot, so there's nothing to wait for). The
           caller surfaces the gateway's own journal as the cause.
      ``rt.HEALTH_FOREIGN`` = the port answers, but another process owns it, so
           this pod's gateway cannot have bound it.
      :data:`HEALTH_NO_CREDENTIAL` = the pod was STILL serving when the budget
           expired, having published no internal-API credential for long enough to
           rule out a gateway that had only just bound, so the mint that follows
           this wait could never have succeeded.
      :data:`HEALTH_SERVING_TOO_BRIEFLY` = the pod was serving on the final poll but
           for less than that span, so the budget is what ran out rather than the
           credential write. The caller reports a slow boot and names the budget.

    Serving is NOT the whole readiness signal, which is why the credential is part
    of the success condition rather than something the caller checks afterwards. A
    gateway publishes its credential only after its listener is bound, so from the
    bind until the end of the remaining startup work the port is already answering
    while the credential does not exist yet. ``up`` mints immediately after this
    wait returns, so a wait that stopped at the HTTP status would hand the mint a
    pod whose credential is still unwritten and die with "no internal-API
    credential ... is it running?" -- on a loaded runner, intermittently, naming a
    cause ("is it running?") that is the opposite of true.

    A pod IS the worktree's gateway, so a dead gateway is a real, expected signal —
    we just want it fast and clearly attributed, not a silent timeout. ``tries``
    is the WALL-CLOCK budget in seconds, enforced by a monotonic deadline
    (default ``POD_HEALTH_WAIT_SECS_DEFAULT``, overridable via `pod up
    --wait-secs` or ``KIROCREW_POD_HEALTH_SECS``): polls run about once per
    second when the port answers fast, and a slow probe (a bound-but-silent
    socket eats the probe's own timeout) shortens the remaining sleeps rather
    than stretching the budget, so the total overrun is at most one probe. The
    credential check shares that one budget rather than adding a second timer, so
    the operator's existing knob still bounds the whole wait.
    The exhausted-budget return is 0 only when NOTHING ever answered the
    port: a real HTTP status (the last polled one, or the last one seen
    before the port went silent) is returned whenever the gateway spoke.

    A foreign responder does NOT end the wait on sight. The pod may still be
    starting, and its own gateway may be moments from winning the port back after
    a predecessor releases it, so the loop keeps its existing exit conditions.
    What the flag changes is the ATTRIBUTION: a port already held by somebody else
    is the reason this pod's gateway is crash-looping, so it is reported ahead of
    the generic crash verdict, which would otherwise send the operator to read a
    journal that only says "address already in use".
    """
    saw_foreign = False
    last_http = 0
    serving_since = None
    deadline = time.monotonic() + max(tries, 1)
    while True:
        code = rt.health(cfg, name, port)
        # Timestamped AFTER the probe returns, because that is when the reading is
        # true. `health` does real I/O -- a connect, a request, and an ownership
        # lookup once something answered -- so a timestamp taken before it would
        # credit the probe's own latency as time this pod spent serving. The span
        # below is what separates "watched it serve and still found no credential"
        # from "it had no chance to publish yet", and inflating it by the probe
        # latency lets a single slow 200 satisfy the span on its own, reporting a
        # slow boot as a credential failure.
        observed = time.monotonic()
        serving = code in (200, 401, 403)
        # Contiguous, so a gateway that served, dropped and came back is timed from
        # the comeback rather than credited with the gap.
        serving_since = (
            (serving_since if serving and serving_since is not None else observed)
            if serving
            else None
        )
        live = rt.published_credential(cfg, name, port)
        if serving and live and live != superseded:
            return code
        if code == rt.HEALTH_FOREIGN:
            saw_foreign = True
        elif serving:
            # Cleared only on POSITIVE proof of ownership, which a serving code is
            # not. `health` downgrades to HEALTH_FOREIGN only when `port_owner`
            # PROVES a foreign responder, and returns the status unchanged for
            # ``OWNER_UNPROVEN`` -- a pid on the port with no fresh record behind it,
            # which is what a failed or unavailable listener lookup leaves. Treating
            # "not provably foreign" as "provably ours" would clear the latch for a
            # still-foreign port whose attestation merely stopped working, which is
            # the reverse of what this latch is for. `port_owner` is asked directly,
            # and only while the latch is set, so the extra attestation is confined to
            # the one path whose verdict can change and the common boot pays nothing.
            if saw_foreign and rt.port_owner(cfg, name, port) == rt.OWNER_POD:
                saw_foreign = False
        elif code > 0:
            # Any real HTTP answer is remembered: a gateway that served an
            # error and then went silent DID answer, and reporting 0 for it
            # would misattribute a broken health route as a slow boot.
            #
            # Only NON-serving codes reach this line, which the branch above now
            # guarantees structurally, and that restriction is load-bearing rather
            # than tidy. Membership in ``(200, 401, 403)`` is the caller's whole
            # success test, so remembering a serving code here would let the
            # exhausted-budget return hand back a 200 for a pod that has stopped
            # serving and has no credential -- reported as success, straight into a
            # mint that cannot work.
            last_http = code
        state, restarts = rt.unit_state(cfg, name)
        # failed = exited non-zero and not restarting; restarts>0 = crash-looping.
        #
        # Not consulted while the port is SERVING. This fast-fail exists to stop
        # waiting on a gateway that is not coming up, and a gateway that is
        # answering has come up -- so the two readings cannot both be acted on.
        # ``restarts`` is the unit's CUMULATIVE NRestarts and nothing in this
        # package resets it, so a pod that ever auto-restarted carries it forever:
        # once the credential joined readiness, a healthy recovered pod serving
        # without its new credential yet would take the crash verdict, and the
        # caller's cleanup would delete the home of the pod that had just
        # recovered. While serving, the only honest outcomes are success and the
        # credential verdict, both reached below.
        if not serving and (state == "failed" or restarts > 0):
            return rt.HEALTH_FOREIGN if saw_foreign else -1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Re-read before attributing anything, because every other reading in
            # this iteration predates the work the iteration did: the credential was
            # read at the top, and `health` and `unit_state` each do real I/O whose
            # latency is exactly the window this wait exists to cover. A credential
            # published inside that window is present but unseen, and the verdicts
            # below would then report "the gateway published none" about a pod that
            # had -- and the caller tears down a pod it started over an empty home on
            # that word. One file read is the whole cost of resting the verdict on
            # the freshest observation available at the moment it is made.
            live = rt.published_credential(cfg, name, port)
            if serving and live and live != superseded:
                return code
            if saw_foreign:
                return rt.HEALTH_FOREIGN
            # Ahead of the bare status, for the same reason FOREIGN is: a pod
            # STILL serving at the deadline with no credential is not a slow boot,
            # and returning its 200 would send the caller on to a mint that cannot
            # succeed.
            #
            # Keyed on this LAST poll, and deliberately not on a sticky "was ever
            # seen serving" flag: a latch would mean a gateway that answered 200
            # once inside the very bind-to-credential window this wait exists for,
            # and then died or started serving 5xx for the remaining budget, still
            # exhausted into the credential verdict -- the one attribution that
            # says the gateway is healthy.
            #
            # The span requirement is the other half. A gateway that binds in the
            # final second of its budget is serving on the last poll while having
            # had no chance at all to publish, and the honest reading of that is a
            # slow boot whose remedy is a bigger budget. Requiring the serving
            # state to have survived a full poll interval means the verdict rests
            # on having watched the pod serve, waited, and still found no
            # credential.
            # Keyed on the start timestamp rather than on `serving`, which is the same
            # condition one inference away: `serving_since` is set exactly when
            # serving and cleared otherwise, so testing it directly is both equivalent
            # and the honest predicate -- a span cannot be measured without the
            # instant it started.
            if (
                serving_since is not None
                and (time.monotonic() - serving_since) >= _MIN_SERVING_SPAN_SECS
            ):
                return HEALTH_NO_CREDENTIAL
            if serving:
                # Serving too briefly to blame the credential write. Its own code
                # must still not be returned, because membership in
                # ``(200, 401, 403)`` is the caller's whole success test and this
                # pod has no credential -- but neither may it become ``last_http``,
                # which is 0 here and reads as "nothing ever answered". The caller
                # needs the distinction to reach for a bigger budget instead of
                # sending the operator after a dead process.
                return HEALTH_SERVING_TOO_BRIEFLY
            return code or last_http
        time.sleep(min(1.0, remaining))


def _wait_escalation_hint(name: str, wait_secs: int) -> str:
    """How to give a slow pod more time, or why there is no more to give.

    ``_health_wait_secs`` clamps to :data:`_POD_HEALTH_WAIT_CEIL_SECS`, so advice
    naming a value above the ceiling sends the operator to retry the budget that
    just failed. The question is therefore whether THIS budget is already at the
    ceiling -- not whether doubling it would exceed one, which is true for every
    budget past the halfway mark and would withhold the real remedy from, say, a
    2000s wait the resolver honours verbatim. Below the ceiling there is always
    more to ask for, so the advice names the smaller of double and the ceiling,
    which is the larger value the resolver will actually honour.

    Shared by both exhausted-budget verdicts because the arithmetic is what was
    wrong, not either message.
    """
    if wait_secs >= _POD_HEALTH_WAIT_CEIL_SECS:
        return (
            f"The budget is already at its {_POD_HEALTH_WAIT_CEIL_SECS}s ceiling, so a "
            f"longer wait cannot be requested -- this is the worktree's own boot time "
            f"to fix."
        )
    raised = min(wait_secs * 2, _POD_HEALTH_WAIT_CEIL_SECS)
    return (
        f"Raise the wait with `kirocrew pod up {name} --wait-secs {raised}` or "
        f"{POD_HEALTH_WAIT_SECS_ENV}={raised}."
    )


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


def _require_up_user_bus(name: str) -> None:
    """Diagnose Linux user-bus reachability before pod-up preparation.

    launchd and Task Scheduler keep their established gates at the operations
    that use them. Running those capability probes here would mutate host
    service state before pod-up reaches its platform-specific seam.
    """
    if not rt.IS_LINUX:
        return
    try:
        rt.require_backend()
    except rt.PodError as exc:
        _audit("pod.up", "failure", f"name={name}", error=str(exc)[:120])
        raise


def _up(cfg: PodConfig, args: argparse.Namespace) -> None:
    name = rt.validate_name(args.name)
    _require_up_user_bus(name)
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
        no_embeddings = bool(getattr(args, "no_embeddings", False))
        if no_embeddings:
            env_updates["EMBEDDINGS"] = "0"
            boot_flags.append("--no-embeddings")

        # Read the unit's state BEFORE choosing a port, and inside the mutex: the
        # two questions are one decision. An `up` against an already-active pod is
        # a restart, and that pod's port is legitimately busy BECAUSE IT OWNS IT --
        # probing would find it taken and move a running pod out from under every
        # reader. Only a pod that is not running is choosing a port at all.
        was_active = rt.is_active(cfg, name)
        home_was_populated = _home_holds_state(cfg, name)
        # Empty on the already-active path and set only where a gateway is really
        # started (see the capture beside `start_pod`). An already-active pod is not
        # a new generation, so its current credential is the correct one and the
        # health wait must accept it rather than wait it out for the whole budget.
        superseded_credential = ""
        # Set only where `start_pod` actually returned success, so it records what
        # THIS invocation did rather than what a probe inferred about the unit. The
        # failed-wait cleanup keys on it because that cleanup can delete a home.
        started_here = False
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
                # Read the OUTGOING credential before the gateway can replace it:
                # this is the value the health wait must refuse to accept as proof
                # that the generation we are about to start has published its own.
                # A crashed pod leaves its predecessor's secret behind (nothing
                # clears it), so without this capture the wait is satisfied by the
                # dead generation on its very first poll. Read here rather than at
                # the top of `_up` because the port is only final once allocation
                # has run, and the credential is keyed by port.
                superseded_credential = rt.published_credential(cfg, name, port)
                cp = rt.start_pod(cfg, name)
                if cp.returncode != 0:
                    _audit(
                        "pod.up",
                        "failure",
                        f"name={name} port={port}",
                        error="backend start failed",
                    )
                    _die(f"starting pod {name} failed: {(cp.stderr or '').strip()}")
                started_here = True
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
        # Record boot-time settings: a pod in `yolo` auto-approves every tool, one
        # with the scheduler on runs work unattended, and one without embeddings
        # answers search from a different index than a normal pod -- so the audit
        # trail must say so rather than recording only that a pod came up. Mark the
        # requested-but-not-yet-effective case: `boot` reads these once at start,
        # so a setting recorded against a live pod has not applied yet.
        # `embeddings=off` is keyed on what the pod boots WITH, not on this command's
        # flag. The merge-preserving env file keeps EMBEDDINGS=0 from an earlier `up`,
        # so a re-up without the flag boots the same embedding-light pod; and a
        # KIROCREW_SKIP_MODEL_DOWNLOAD=1 already in this environment is what
        # pod_context hands every `pod exec` and what an inheriting boot carries,
        # with no key ever written. Either pod answers search from a different
        # index, and a row that said nothing would contradict the journal line
        # `boot` prints for both (it keys on the effective env the same way).
        embeddings_off = (
            no_embeddings
            or rt.embeddings_disabled(rt.read_env_file(cfg, name))
            or os.environ.get(rt.SKIP_MODEL_DOWNLOAD_ENV) == "1"
        )
        resources = f"name={name} port={port}"
        if approval:
            resources += f" approval={approval}"
        if crons:
            resources += " crons=on"
        if embeddings_off:
            resources += " embeddings=off"
        if boot_flags and was_active:
            resources += " applied=next_boot"
        _audit("pod.up", "allowed", resources)

        wait_secs = _health_wait_secs(args)
        code = _wait_healthy(cfg, name, port, tries=wait_secs, superseded=superseded_credential)
        if code not in (200, 401, 403):
            # A pod IS the worktree's own gateway. If it won't boot, that's a broken
            # worktree build (bad import / config / unbuilt dist) — NOT a pod-tooling
            # fault. Surface the gateway's own journal so the dev fixes the real cause,
            # and stop a unit this command started so we don't leak a crash-looping
            # service — stopping only, when the home predates us, because reclaiming
            # it would delete state this command did not create.
            #
            # This failure cleanup runs INSIDE the same mutex hold as our start:
            # released between the two, a down + replacement up could interleave
            # during the health wait, and the stop below would then act on the
            # REPLACEMENT pod. Holding the lock across the whole boot transaction
            # means the pod stopped here can only be the one we started.
            tail = rt.recent_journal(cfg, name, 30)
            print(tail, file=sys.stderr)
            # Attribution must be read BEFORE stop_pod tears the unit down:
            # after the stop, unit_state cannot tell a slow boot from a dead
            # gateway. Positive evidence only, and the two inputs that can carry
            # it are kept apart. HEALTH_SERVING_TOO_BRIEFLY is the port itself
            # answering, which needs no corroboration. code == 0 means nothing
            # ever answered, so liveness has to come from the unit: an
            # active/activating unit with zero restarts is the inverse of the
            # crash signal _wait_healthy returns -1 on, while "unknown" and
            # "inactive" keep the timeout verdict. A real HTTP error like 404/5xx
            # is neither: the gateway IS serving and its health route is broken,
            # which more wait can never fix, so it keeps the timeout verdict too.
            still_starting = False
            if code == HEALTH_SERVING_TOO_BRIEFLY:
                # No unit read at all: the port answering on the final poll is
                # stronger evidence the gateway is alive than anything the unit can
                # say, and it is evidence the unit read actively contradicts --
                # `restarts` is cumulative with nothing in this package resetting it,
                # so a recovered pod fails `restarts == 0` forever and would be
                # reported as never healthy while it was answering 200.
                still_starting = True
            elif code == 0:
                state, restarts = rt.unit_state(cfg, name)
                still_starting = state in ("active", "activating") and restarts == 0
            # Torn down ONLY when this invocation both started the gateway and
            # found no state predating it. Two recorded facts, because a liveness
            # probe cannot answer either question: `is_active` shells
            # `systemctl is-active --quiet`, which succeeds only for `active`, so a
            # unit in `activating` or in `Restart=on-failure` backoff -- a
            # crash-loop this package documents as the ordinary case -- reads as not
            # running, and keying the stop on that reads a PRE-EXISTING pod as one
            # this command created. `stop_pod` reaches `cleanup_home`, which rmtree's
            # the isolated HOME with its sessions and config and nothing restores it,
            # so the guard has to rest on facts that cannot be wrong about the past:
            # `started_here` is set where `start_pod` actually succeeded, and
            # `home_was_populated` was read before it and fails closed. A pod that is
            # genuinely broken but not ours to delete is `pod down`'s business, and
            # the operator still gets the verdict and the journal below.
            # Two questions, deliberately not one. Stopping is owed whenever THIS
            # invocation started the gateway: the unit is `Restart=on-failure` with
            # `RestartSec=5` and no `StartLimit` override, so a crash outside
            # `RestartPreventExitStatus` -- a gateway that raises at import exits 1,
            # which is not a terminal boot code -- respawns every five seconds for as
            # long as nobody stops it. The 5s gap never fills systemd's default
            # ten-second burst window, so the rate limiter never retires it either.
            # Walking away from a unit this command started is a leak.
            #
            # Deleting is permitted only when this invocation ALSO created the home.
            # `stop_pod` reaches `cleanup_home`, which rmtree's the isolated HOME with
            # its sessions and config and nothing restores it, so state that predates
            # the command must survive a failed wait.
            #
            # Neither question is answerable from `is_active`: it shells
            # `systemctl is-active --quiet`, which succeeds only for `active`, so a pod
            # in `activating` or in restart backoff reads as not running and a
            # pre-existing crash-looping pod would be treated as one this command
            # created. `started_here` and `home_was_populated` are facts about what
            # happened; the probe is a guess about the past.
            destroyed = started_here and not home_was_populated
            halted = started_here and home_was_populated
            halt_failed = ""
            stop_failed = ""
            if destroyed:
                # The return code is inspected for the same reason `halt_pod`'s is:
                # the notice below claims this command tore the pod down, and this
                # call is the only evidence for that claim. A non-zero `stop_pod`
                # means the gateway may still be live or its HOME survived, and an
                # operator told it was cleaned up would leave a crash-looping unit
                # running on a port they believe is free.
                stop = rt.stop_pod(cfg, name)
                if stop.returncode != 0:
                    detail = (stop.stderr or stop.stdout or "").strip()
                    stop_failed = (
                        f" Tearing it down FAILED (rc={stop.returncode}), so the gateway "
                        f"may still be running and its isolated home may survive: "
                        f"{detail or 'no diagnostic'}"
                    )
            elif halted:
                # The return code is inspected rather than discarded, because the
                # notice below claims the service was stopped and this call is the
                # only evidence for that claim. `halt_pod` returns non-zero for the
                # cases that matter most -- a Linux reload it refused to proceed
                # without, a launchd bootout or a Windows retirement it could not
                # confirm -- and every one of them leaves the gateway RUNNING. An
                # operator told "stopped" would then leave a crash-looping unit in
                # place believing it was handled, which is the failure this whole
                # branch exists to prevent.
                halt = rt.halt_pod(cfg, name)
                if halt.returncode != 0:
                    detail = (halt.stderr or halt.stdout or "").strip()
                    halt_failed = (
                        f" Stopping it FAILED (rc={halt.returncode}), so the gateway may "
                        f"still be running: {detail or 'no diagnostic'}"
                    )
            # Computed ONCE and appended to every verdict below, because the guard is
            # verdict-independent and the notice has to be too. Saying nothing on four
            # of five exits leaves the operator believing `up` cleaned up after itself,
            # and the next thing they do is derived from that belief -- rerunning, or
            # reallocating the port under a live gateway. Each branch states only what
            # this command DID, never what the unit is doing: a pod that has genuinely
            # failed is not running, so claiming it is would replace one false
            # impression with another.
            if destroyed:
                # Silent on success: a pod this command created and then removed
                # leaves the operator nothing to act on, so there is nothing to say.
                left_running = (
                    ""
                    if not stop_failed
                    else f"{stop_failed} Retire it with `kirocrew pod down {name}`."
                )
            elif halted:
                left_running = (
                    " The gateway this command started has been stopped, but its "
                    "isolated home was KEPT because it held state beforehand. Reclaim "
                    f"it deliberately with `kirocrew pod down {name}`.{halt_failed}"
                    if not halt_failed
                    else (
                        " Its isolated home was KEPT because it held state beforehand."
                        f"{halt_failed} Retire it with `kirocrew pod down {name}`."
                    )
                )
            else:
                left_running = (
                    " This pod was NOT stopped: this command did not start it, and "
                    "tearing it down would delete an isolated home it did not create. "
                    f"Retire it deliberately with `kirocrew pod down {name}`."
                )
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
                    f"{left_running}"
                )
            if code == -1:
                _die(
                    f"{name}: the worktree's gateway failed to start (see journal above). "
                    f"This is the worktree build, not pod — fix it, then `kirocrew pod up {name}` again."
                    f"{left_running}"
                )
            if code == HEALTH_NO_CREDENTIAL:
                # Reported ahead of the slow-boot verdict below, which this would
                # otherwise be mistaken for: the gateway was serving when the budget
                # expired, so "still starting" is false. Two causes reach here and
                # the message names both, because the pod's own journal is what
                # separates them: a failed credential write, or one that simply
                # landed after the budget.
                _audit(
                    "pod.up",
                    "failure",
                    f"name={name} port={port}",
                    error="serving but no internal-API credential published",
                )
                _die(
                    f"{name}: the gateway was still serving on :{port} when the "
                    f"{wait_secs}s readiness budget expired, but had published no "
                    f"internal-API credential, so this pod cannot be driven. Its "
                    f"journal is above: a write error there is the cause. If it "
                    f"shows none, the gateway published late rather than failing. "
                    f"{_wait_escalation_hint(name, wait_secs)} "
                    f"This is the worktree's gateway, not pod.{left_running}"
                )
            if still_starting:
                # Slow boot, not a dead gateway: the process is alive and readiness
                # did not complete inside the budget. Deliberately does NOT claim the
                # port is silent, because this branch covers two states and only one
                # of them is: it is also reached by HEALTH_SERVING_TOO_BRIEFLY, where
                # the gateway was serving on the final poll but for less than the span
                # the credential verdict requires. Asserting "/api/health not yet
                # answering" is false for that one, and false in the direction that
                # sends the operator to look for a dead process. A bigger budget is
                # the remedy for both.
                _audit(
                    "pod.up",
                    "failure",
                    f"name={name} port={port}",
                    error="health wait exhausted while gateway alive",
                )
                _die(
                    f"{name}: gateway still starting after {wait_secs}s on :{port} "
                    f"(process alive; readiness did not complete -- it had not answered "
                    f"/api/health with a published credential for long enough to judge). "
                    f"{_wait_escalation_hint(name, wait_secs)}{left_running}"
                )
            _die(
                f"{name}: gateway never became healthy on :{port} within timeout "
                f"(see journal above; check the worktree's gateway start path)."
                f"{left_running}"
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
    if getattr(args, "no_token", False):
        # The caller (the gateway's agent pod surface) will mint in-process. A
        # sandboxed `pod up` child runs in its own user namespace, which the pod
        # refuses to certify as the local owner (member_owner_token_refused), so
        # minting here would fail the whole boot for a caller that never wanted
        # this token. Skip the mint entirely: an empty `token` is the handle's
        # documented "no credential" signal and the gateway supplies its own.
        _audit("pod.token", "skipped", f"name={name} port={port} reason=no-token")
    else:
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
        elif getattr(args, "no_token", False):
            print("  token    : (skipped by request: --no-token)")
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
        # torn down. Windows is the same shape: a task whose gateway already died
        # leaves no supervised pid, so `was_up` is False while the task itself is
        # still registered and must be deleted before the HOME is reclaimed.
        if cp.returncode != 0 and (was_up or had_home or rt.IS_MACOS or rt.IS_WINDOWS):
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
    _print_refusals(cfg)
    _print_orphans(cfg, orphans)


def _print_refusals(cfg: PodConfig) -> None:
    """Report pods whose LAST boot refused terminally.

    ``PodConfig.refusal_file``'s whole justification is that a terminal refusal is
    visible in different amounts on the two service managers -- systemd leaves the
    unit ``failed``, while launchd sees the exit-0 that stops its restart loop and
    reads it as an ordinary clean exit -- and that ``pod ls`` should report the same
    fact either way. It did not: a refused pod is not running, so it fell out of the
    listing entirely and ``ls`` printed "no pods running". The note existed with no
    reader, and a pod that silently vanishes from ``ls`` is exactly how a boot
    failure hides.

    Rendered as its own section rather than a row in the main table, mirroring
    :func:`_print_orphans`: a refused pod has no port and no health, so a table row
    would have to invent both.
    """
    try:
        names = sorted(p.name[: -len(".refused")] for p in cfg.pods_dir.glob("*.refused"))
    except OSError:
        return
    refused = [(n, rt.refusal_reason(cfg, n)) for n in names]
    refused = [(n, why) for n, why in refused if why]
    if not refused:
        return
    print(
        f"\n{len(refused)} pod(s) REFUSED to boot — the last attempt stopped on a "
        "safety check and did not start a gateway:"
    )
    for name, why in refused:
        print(f"  {name:<26} {why}")
        print(f"  {'':<26} clear: kirocrew pod down {name}")


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
            # delete time for writers that bypass the mutex. Windows: its
            # per-pod `.cmd` wrapper carries exactly the same meaning.
            if rt.IS_MACOS and rt.launchd.plist_path(cfg, name).exists():
                return "skipped", "pod is now installed", "denied", "pod is now installed"
            if rt.IS_WINDOWS and rt.win_backend.task_script_path(cfg, name).exists():
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
    # A bare `systemctl is-active` answers "down" on a host with no user
    # manager, so gate first and let the dispatch layer print the refusal.
    if rt.IS_LINUX:
        rt.require_backend()
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
    if rt.IS_MACOS or rt.IS_WINDOWS:
        # Neither launchd nor Task Scheduler has a journal; the plist / the .cmd
        # wrapper route stdout/stderr to files and recent_journal tails them.
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
    # Audit the HONEST code, before any service-manager translation below.
    _audit("pod.boot", "failure", f"name={args.name}", error=f"exit={rc}")
    # launchd has no RestartPreventExitStatus: its only restart discriminator is
    # the success/failure axis, and this backend's KeepAlive restarts on NON-ZERO.
    # A terminal refusal must therefore exit 0 or launchd re-runs it every 5s.
    # ``rt.terminal_exit_code`` is the record-CONDITIONAL gate -- it translates only
    # when the refusal note actually landed, so a refusal that could not be recorded
    # keeps its honest non-zero instead of looking like a clean exit. Do NOT call
    # ``launchd.launchd_exit_code`` directly here; it states the platform semantics
    # but knows nothing about whether the record exists. Windows needs no
    # translation at all: Task Scheduler never restarts a non-zero exit, so the
    # honest code is already the terminal one. The branch below says so, and
    # `test_the_runtime_wrapper_does_not_translate_on_windows` pins it there --
    # which is where a change that adds a restart policy would have to look.
    exit_code = rt.terminal_exit_code(cfg, args.name, rc)
    if exit_code != rc:
        print(
            f"kirocrew-pod: exiting 0 instead of {rc} so launchd does not restart "
            f"into the same refusal every 5s; recorded at {cfg.refusal_file(args.name)}"
        )
    sys.exit(exit_code)


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
