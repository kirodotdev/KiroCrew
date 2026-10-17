"""Who serves a pod's port: gateway PID attestation against the service manager.

:func:`port_owner` proves ownership from two independent pod-owned facts -- the
gateway pid sidecar in the pod's isolated home, which proves its own freshness
through a start-time identity, and the service manager's current ``MainPID`` --
and uses loopback listener attribution only as corroboration. Every caller that
would hand the pod a credential, or report it healthy, goes through this verdict.

``main_pid`` and the platform flags are read from :mod:`kiro_crew.pod.runtime`
at call time, the namespace the pod suite patches. The process and listener
probes this module imports are its own seams: the runtime module forwards reads
and patches of them here.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.instances import run_marker
from kiro_crew.platform_compat import (
    attributed_descendants,
    find_port_listeners,
    listening_pid_tool_available,
    loopback_owner_pids,
    process_start_time,
)
from kiro_crew.pod import runtime
from kiro_crew.pod.config import PodConfig

# --------------------------------------------------------------------------- #
# Port ownership — WHO answers the pod's port, not merely whether anyone does.
#
# A pod's port is derived, not allocated: `base + (cksum(name) % 199) + 1` maps
# every pod name into 199 slots, and `PORT=` in the per-pod env file can pin any
# port by hand. So two pods colliding on one port is an ordinary event, and the
# live gateway is reachable on the same loopback interface. Whoever binds first
# wins; the loser's gateway exits "address already in use" and the unit
# crash-loops behind it.
#
# A bare `GET /api/health` cannot tell those apart. `{"ok": true}` is the same
# answer from any Kiro Crew gateway on the host, and its identity fields say
# `app=kirocrew` plus a version — true of the squatter as well. Reading a 200 as
# "this pod is up" therefore reports a crash-looping pod as healthy and points
# the operator's browser at somebody else's instance.
#
# `instances/run_marker` states the rule this module was breaking: it
# "deliberately does not offer a bare 'is something listening' helper, so no
# caller can mistake reachability for identity". The pod probe is now held to it
# — the reachability probe is private and every caller goes through `health`.
# --------------------------------------------------------------------------- #
#: Proven: the pod's own gateway process holds the port.
OWNER_POD = "pod"


#: Proven: something OTHER than this pod holds the port (another pod, the live
#: gateway, an unrelated process).
OWNER_FOREIGN = "foreign"


#: Not proven: the gateway pid record and the service manager's MainPID could
#: not be attested as the same live process. A missing listener-lookup tool does
#: not cause this on its own (the record verdict is returned as-is); and
#: :func:`kiro_crew.pod.runtime_client.mint_token` refuses with
#: ``PodOwnershipUnproven`` rather than falling back.
OWNER_UNPROVEN = "unproven"


def _pod_pid_record_path(cfg: PodConfig, name: str, port: int) -> Path:
    """Path of pod *name*'s gateway pid sidecar inside its isolated home."""
    return cfg.home_dir(name) / run_marker.RUN_DIR_NAME / run_marker.pid_file_name(port)


def _pod_recorded_pid(cfg: PodConfig, name: str, port: int) -> int | None:
    """Gateway PID recorded in pod *name*'s isolated home, PROVEN to still name
    that same process, or ``None``.

    A bare pid is not an identity. ``clear_marker`` runs only on a graceful
    shutdown, so a crashed or SIGKILLed pod leaves its sidecar behind, and the
    number in it can afterwards be recycled onto an unrelated process. The
    gateway therefore records its start-time identity in a ``.start`` sidecar
    beside the pid (``run_marker.pid_start_token``), and this reader re-derives
    that identity live: a recycled pid answers with its OWN start time, which
    cannot match the one the pod's gateway recorded.

    Fails CLOSED on every way of not knowing -- no record, no start identity (a
    pod whose checkout predates the binding), or a host that will not report a
    start time at all. An unproven record must read as "no record", never as one
    that agrees. Windows is NOT in that last group: ``pid_start_token`` reads the
    process creation ``FILETIME`` there, which is what lets a pod on that platform
    prove ownership at all.
    """
    record = run_marker.read_pid_record_path(_pod_pid_record_path(cfg, name, port))
    if record is None:
        return None
    pid, recorded_start = record
    if not recorded_start:
        return None
    live_start = run_marker.pid_start_token(pid)
    if not live_start or live_start != recorded_start:
        return None
    return pid


def _unproven_remedy(cfg: PodConfig, name: str, port: int) -> str:
    """How to fix an unproven ownership verdict -- the two causes differ.

    A record that is absent, malformed, or names a pid whose start identity no
    longer matches is crash residue or a stale generation, and a restart writes a
    fresh, provable one. A record that carries NO start identity is a different
    failure with the opposite remedy: a pod's gateway is its checkout's own venv
    binary, so a worktree branched before the start-identity sidecar existed
    writes no token, and restarting it writes none either -- telling the operator
    to restart would send them round a loop that cannot terminate.
    """
    record = run_marker.read_pid_record_path(_pod_pid_record_path(cfg, name, port))
    if record is not None and not record[1]:
        return (
            f"The record names a pid but carries no start identity, and a restart "
            f"cannot add one: a pod's gateway is its worktree's own venv binary, so "
            f"this checkout predates the start-identity sidecar. Update that "
            f"worktree to a build that writes one and re-provision it "
            f"(`kirocrew pod provision {name}`), then `kirocrew pod down {name} && "
            f"kirocrew pod up {name}`."
        )
    return (
        f"A record left behind by a crash cannot attest, so restarting it is what "
        f"re-establishes the proof: `kirocrew pod down {name} && "
        f"kirocrew pod up {name}`."
    )


def port_owner(cfg: PodConfig, name: str, port: int) -> str:
    """Attest who serves *port* for pod *name*.

    The proof is agreement between two independent pod-owned facts: the gateway
    PID sidecar in the pod's isolated ``0700`` home and the service manager's
    current ``MainPID`` (or launchd PID). The pod unit is ``Type=simple`` and
    execs the gateway in place, so that PID is the process that bound the port,
    and the sidecar is written only AFTER that bind succeeds. A pod that lost
    the bind race therefore has no agreeing record to offer.

    That agreement is only worth anything because the record proves its own
    freshness: :func:`_pod_recorded_pid` answers with a pid ONLY when the
    process it names still has the start-time identity the record was written
    with, so a sidecar left behind by a crash cannot attest once its pid has
    been recycled. Without that binding the number alone could name somebody
    else, which is the only reason listener attribution was ever load-bearing.

    Listener attribution is CORROBORATION, not a precondition. Its view is
    scoped to the 127.0.0.1 listener the HTTP client reaches, so a pid on the
    port that is not ours is positive proof of a foreign responder and outranks
    any record. But when the host has no usable listener evidence -- no lookup
    tool, a lookup that failed, or a lookup that simply could not see the socket
    -- a provably fresh PID-record/MainPID agreement is sufficient. That is the
    normal supported path on a minimal Linux host, and on any host where the
    caller cannot see another process's sockets (an unprivileged process asking
    ``lsof`` about a gateway started by the user's service manager, which is
    exactly how ``pod api`` runs). Requiring attribution there would withhold
    the credential from every healthy pod forever.

    Listener attribution is still never sufficient ON ITS OWN: a pid that holds
    the port but has no fresh record behind it stays :data:`OWNER_UNPROVEN`.

    **Windows reaches this the same way**, which it did not before pods had a
    backend there. The proof needs exactly two things, and both now answer on
    win32: ``run_marker.pid_start_token`` has a Windows leg (the process creation
    ``FILETIME``, read through a query-only handle), and :func:`main_pid` reads the
    pid ``windows.supervise_gateway`` records. Keeping the old blanket refusal here
    would not have been strictness, it would have been unsatisfiable — ``pod up``
    mints a token and :func:`mint_token` requires positive proof, so every healthy
    Windows pod would have been refused its own credential forever. Listener
    corroboration works there too (``netstat`` via ``trusted_system_bin``).

    **On Windows the recorded pid and the binding pid are different processes, and
    both are the pod.** A pip console script is an ``.exe`` launcher stub that
    starts the interpreter as a child and waits, so ``supervise_gateway`` records
    the stub while that child binds the port, and the gateway's own pid sidecar
    names that child. The Windows leg therefore reads the stub's descendants once
    (:func:`kiro_crew.platform_compat.attributed_descendants`) and accepts them in
    both places the proof compares pids: the sidecar's pid attests when it is the
    stub or one of its descendants, and a listener inside that tree corroborates.
    That widens the accepted set DOWNWARD only: a sidecar or a listener outside
    this pod's tree still fails exactly as before.

    **Every edge of that tree is attributed by creation order, or the widening
    would point the wrong way.** Windows never invalidates a snapshot's
    ``th32ParentProcessID`` when the parent dies, so once that number is recycled
    to the stub, processes the stub never spawned are listed beneath it — and a
    FOREIGN listener inside such a phantom subtree would be read as this pod
    holding its own port, which is the single question this function answers.
    :func:`kiro_crew.platform_compat.attributed_descendants` applies that rule
    (``created_after``) to every edge, not just the root, and a candidate
    whose creation time cannot be read is dropped as unattributable — failing
    toward ``OWNER_FOREIGN`` / ``OWNER_UNPROVEN`` rather than toward a false claim
    of ownership.
    """
    try:
        recorded = _pod_recorded_pid(cfg, name, port)
        ours = runtime.main_pid(cfg, name)
    except Exception:
        return OWNER_UNPROVEN
    # On Windows the gateway's own sidecar names the interpreter that bound the
    # port, while ``ours`` is the launcher stub the supervisor recorded, so the
    # two agree only through the process tree. One snapshot serves both this
    # attestation and the listener corroboration below.
    #
    # The snapshot's parent pids are STALE BY DESIGN: Windows keeps a dead
    # parent's number on its children, so once that number is recycled to the
    # stub, processes it never spawned appear beneath it. Unfiltered, that is a
    # widening in the wrong direction — a FOREIGN listener sitting in such a
    # phantom subtree would be read as this pod holding its own port, which is
    # the one thing this function exists to decide. Every edge is therefore
    # attributed by creation order through the same rule ``windows.stop`` uses,
    # so a candidate that predates the stub is not this pod's child. A child
    # whose creation time cannot be read is not attributable and is dropped,
    # which fails toward OWNER_FOREIGN/UNPROVEN rather than toward a false claim
    # of ownership.
    tree: set[int] = set()
    if runtime.IS_WINDOWS and ours is not None:
        try:
            ours_token = process_start_time(ours)
            if ours_token:
                # EVERY edge attributed, not just "created after the pod's gateway".
                # This set decides whether a listener is THIS pod, and the answer
                # gates minting a credential: a stale orphan under a recycled
                # intermediate pid also postdates the gateway, so a root-only
                # comparison could call a foreign listener ours and send it the
                # pod's secret. The walk drops an unattributable child with its
                # subtree, which fails toward UNPROVEN rather than toward a false
                # claim of ownership.
                tree = set(attributed_descendants(ours, ours_token))
        except Exception:
            tree = set()
    attested = (
        recorded is not None
        and ours is not None
        and (recorded == ours or (runtime.IS_WINDOWS and recorded in tree))
    )
    verdict = OWNER_POD if attested else OWNER_UNPROVEN

    if not listening_pid_tool_available():
        return verdict
    try:
        pids = set(loopback_owner_pids(find_port_listeners(port)))
    except Exception:
        return verdict
    if not pids:
        return verdict
    if ours is not None and ours in pids:
        return verdict
    if runtime.IS_WINDOWS and pids & tree:
        # A console-script `.exe` on Windows is a launcher stub: it starts the
        # interpreter as a CHILD and waits, so the pid `supervise_gateway` records
        # is the stub while the pid that BINDS the port is that child. Both are
        # this pod's tree, and the Job object attached at spawn is what keeps the
        # tree bounded, so a descendant holding the port is the pod holding it.
        # Read from ONE process-table snapshot through the shared helper rather
        # than a new matcher, and only ever widened DOWNWARD: a pid outside the
        # recorded pid's descendants is still a foreign responder below.
        return verdict
    return OWNER_FOREIGN
