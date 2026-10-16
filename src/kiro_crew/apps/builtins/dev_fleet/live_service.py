"""How Dev Fleet finds and drives the live gateway as a service.

Which checkout the running gateway runs from (its own package, the launchd agent's
program, the staged pointer target), which systemd unit or launchd label it runs
under, the restart backend that drives it (systemd, launchd, or a foreground
process), the cached service-active and start-id probes, and the systemd drop-in a
cutover writes. Holds ``_GATEWAY_SERVICE_ACTIVE`` / ``_GATEWAY_SERVICE_CHECK_AT``,
which ``_gateway_service_active`` rebinds. Reached through ``live``, its facade.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.dev_fleet import gateway_service, repository, runtime
from kiro_crew.instances import run_marker
from kiro_crew.service import live_target


def _own_checkout_path() -> str | None:
    """Checkout root the RUNNING process's kiro_crew package resolves into.

    The systemd probe only sees service-managed gateways; a gateway launched
    directly from a feature worktree (and this backend, its subprocess) is
    invisible to it. Our own module path is ground truth for which checkout
    is live code right now -- editable installs resolve
    ``<checkout>/src/kiro_crew/__init__.py``.
    """
    try:
        import kiro_crew as _pkg

        p = Path(_pkg.__file__).resolve()
        for parent in p.parents:
            if (parent / ".git").exists() or (parent / "pyproject.toml").is_file():
                return str(parent)
    except Exception:  # noqa: BLE001 -- identity probe must never crash callers
        return None
    return None


def _launchd_live_worktree() -> str | None:
    """Resolve the live checkout from the launchd agent's live-gateway launcher.

    The launcher is a generated script whose ``exec`` line names the target
    binary; the checkout is that binary's ``.venv`` grandparent. Returns ``None``
    when the launcher is absent (no agent installed) or names something that is
    not a worktree venv binary — e.g. a freshly installed agent still aimed at
    the system-wide ``kirocrew``. ``None`` correctly means "no row is live"
    rather than guessing.
    """
    try:
        script = gateway_service.LaunchdBackend.live_program().read_text()
    except OSError:
        return None
    m = re.search(r"^exec '((?:[^']|'\\'')+)'", script, re.MULTILINE)
    if not m:
        return None
    exe = Path(m.group(1).replace("'\\''", "'"))
    # <checkout>/.venv/bin/kirocrew -> <checkout>
    if ".venv" not in exe.parts:
        return None
    try:
        return str(exe.parents[2].resolve())
    except (OSError, IndexError):
        return None


def _running_checkout() -> Path | None:
    """The checkout this gateway process is EXECUTING from, or None.

    Authoritative where a service definition is not: it is derived from the
    location of the code that is actually loaded, so it needs no service query
    and cannot be fooled by a definition that was never updated. Returns None
    for a packaged/site-packages install, which is not a checkout at all — the
    caller must treat that as "cannot verify" rather than as a mismatch.
    """
    own = repository._own_source_checkout()
    return Path(own) if own else None


def _staged_target() -> str | None:
    """The pointer target when a cutover is staged but NOT yet in effect.

    Non-None means the operator has committed a cutover that the gateway has not
    picked up: the next start lands on this checkout, and until then the running
    image is a different one. The UI renders this as its own persistent state so
    the pending restart survives a dismissed toast or a page reload.

    GATEWAY-LOCAL: reads the pointer file directly, which only the gateway may do.
    ``_make_live`` (gateway-only) calls this; the backend's call sites go through
    :func:`_staged_target_resolved`.
    """
    pointed = live_target.read_target()
    if pointed is None:
        return None
    running = _running_checkout()
    if running is None or repository._same_path(str(pointed), str(running)):
        return None
    return str(pointed)


_GATEWAY_SERVICE_ACTIVE: bool | None = None
_GATEWAY_SERVICE_CHECK_AT: float = 0.0
_GATEWAY_SERVICE_TTL = 30.0
_LIVE_GATEWAY_UNIT = "kirocrew-gateway.service"
# The launchd counterpart of the live systemd unit. Same agent
# `kirocrew service install` writes (kiro_crew.service.common.LAUNCHD_LABEL);
# duplicated as a literal rather than imported so this module keeps importing
# cleanly on hosts where the service package's optional deps are unavailable.
_LIVE_GATEWAY_LABEL = "dev.kirocrew.gateway"


def _gateway_unit_name() -> str:
    """Resolve the systemd unit of the gateway THIS backend belongs to.

    Inside a pod (config home under ``.kirocrew-pods/<name>``) the owning unit
    is the pod template instance — restarting the hardcoded live unit from a
    pod would bounce the user's LIVE gateway across planes.
    """
    try:
        from kiro_crew.config.loader import config_dir

        home = config_dir()
        if home.parent.name == ".kirocrew-pods":
            return f"kirocrew-pod@{home.name}.service"
    except Exception:  # noqa: BLE001 — fall through to the live unit
        pass
    return _LIVE_GATEWAY_UNIT


def _gateway_label() -> str:
    """Resolve the launchd label of the gateway THIS backend belongs to.

    The launchd counterpart of :func:`_gateway_unit_name`, with the same pod
    rule for the same reason: inside a pod the owning agent is that pod's own,
    and kickstarting the live agent from a pod plane would bounce the user's
    LIVE gateway. The label shape mirrors ``pod.launchd.pod_label`` — every
    plane carries its ``unit_prefix`` segment, including the default one.
    """
    try:
        from kiro_crew.config.loader import config_dir
        from kiro_crew.pod.config import DEFAULT_UNIT_PREFIX
        from kiro_crew.pod.launchd import LABEL_PREFIX

        home = config_dir()
        if home.parent.name == ".kirocrew-pods":
            prefix = os.environ.get("KIROCREW_POD_UNIT_PREFIX", DEFAULT_UNIT_PREFIX)
            return f"{LABEL_PREFIX}.{prefix}.{home.name}"
    except Exception:  # noqa: BLE001 — fall through to the live agent
        pass
    return _LIVE_GATEWAY_LABEL


def _gateway_backend() -> "gateway_service.GatewayServiceBackend | None":
    """Build the service backend for this host.

    Constructed per call, never cached: ``platform`` and ``which`` are resolved
    HERE, through this module's globals, so tests can drive platform detection
    by patching ``live.sys`` / ``live.shutil``. Caching the instance would
    freeze the first verdict and
    silently escape those patches.
    """
    return gateway_service.backend(
        runtime._run_cmd,
        unit=_gateway_unit_name,
        label=_gateway_label,
        platform=sys.platform,
        which=shutil.which,
        # Resolved at call time so tests patching these module attributes still
        # control the systemd rendering (see SystemdBackend's docstring).
        dropin_path=_dropin_path,
        dropin_content=_dropin_content,
    )


def _foreground_backend() -> "gateway_service.ForegroundBackend | None":
    """The last-resort foreground restart backend, or ``None`` off-POSIX.

    Constructed per call for the same reason as :func:`_gateway_backend`, and
    ``sys.platform`` is read through this module's globals so tests that patch
    ``live.sys`` keep controlling it. POSIX-only: the detach mechanism
    is a new session standing in for ``systemd-run --collect``, and the hosts
    this exists for (no drivable systemd/launchd) are Linux and macOS; Windows
    keeps the manual-restart advisory. Whether a restart can actually be
    attempted is the backend's ``status()``, not this constructor — callers
    must gate on both :data:`gateway_service.FOREGROUND_ELIGIBLE` and
    ``status() == ok``.
    """
    if sys.platform not in ("linux", "darwin"):
        return None
    return gateway_service.ForegroundBackend(
        marker_ports=run_marker.marker_ports,
        read_pid=run_marker.read_pid,
        read_launcher=run_marker.read_launcher,
        pid_exists=platform_compat.pid_exists,
    )


async def _gateway_service_active() -> bool:
    """Cached check: is the gateway running as a service we can drive?

    Async and routed through the sandboxed ``_run_cmd`` chokepoint: a sync
    ``subprocess.run`` here would block the event loop on cache miss AND
    bypass the spawn-audit sandbox invariant.
    """
    global _GATEWAY_SERVICE_ACTIVE, _GATEWAY_SERVICE_CHECK_AT
    now = time.monotonic()
    if (
        _GATEWAY_SERVICE_ACTIVE is not None
        and (now - _GATEWAY_SERVICE_CHECK_AT) < _GATEWAY_SERVICE_TTL
    ):
        return _GATEWAY_SERVICE_ACTIVE
    svc = _gateway_backend()
    active = False if svc is None else await svc.active()
    if not active:
        # Foreground backend is the last-resort restart path for hosts without
        # a drivable service manager. If eligible, the Restart button and the
        # auto-restart-after-sync flow should still be available — but ONLY
        # when the backend is not confined (status() == STATUS_OK), mirroring
        # the foreground probe in ``live._make_live_inner``.
        status = await _live_user_unit_status()
        if _foreground_eligible(status):
            fg = _foreground_backend()
            if fg is not None and await fg.status() == gateway_service.STATUS_OK:
                active = True
    _GATEWAY_SERVICE_ACTIVE = active
    _GATEWAY_SERVICE_CHECK_AT = now
    return _GATEWAY_SERVICE_ACTIVE


async def _gateway_start_id() -> str | None:
    """Start identity of the live gateway, or ``None``. Delegated per platform.

    On systemd this is ``ExecMainStartTimestampMonotonic``; on launchd it is the
    agent's PID (launchd exposes no monotonic start stamp). See
    ``gateway_service`` for each backend's rationale and caveats.

    Reads ``ExecMainStartTimestampMonotonic`` -- the CLOCK_MONOTONIC microsecond
    stamp of the unit's ExecStart *main* PID. Chosen over a wall-clock stamp or
    ``ActiveEnterTimestampMonotonic`` because it is (a) monotonic, so it can
    only increase and never repeats or goes backwards across a restart even if
    the wall clock is stepped by NTP, and (b) tied to the actual main-process
    spawn, so it changes the instant the NEW gateway process starts -- precisely
    the "the new process is up" signal the restart handshake needs (a unit can
    enter ``active`` before its replacement main PID exists).

    Returns ``None`` when no service manager applies, the probe fails, or the
    manager reports no usable identity (systemd prints ``0`` when no main-start
    stamp is recorded; launchd omits the pid line for a loaded-but-not-running
    agent). Callers
    MUST treat ``None`` as "identity unavailable" and degrade to the legacy
    reload-on-first-response behaviour rather than waiting forever in
    "restarting". Uses ``_gateway_unit_name()`` so it matches whichever unit
    ``_restart_gateway`` / ``_make_live`` actually bounce (pod or live).
    """
    svc = _gateway_backend()
    sid = None if svc is None else await svc.start_id()
    if sid is not None:
        return sid
    # Foreground fallback: on a host where no manager can be driven the
    # handshake still needs an identity that changes when the replacement
    # starts, or a foreground cutover could never be observed to complete. The
    # run-marker pid stands in (see ForegroundBackend). Gated on the same
    # eligibility codes as the foreground restart itself so that a host with a
    # mis-set-up manager (which this app refuses to bounce) does not start
    # advertising an identity for a restart path that will never run.
    if _foreground_eligible(await _live_user_unit_status()):
        fg = _foreground_backend()
        if fg is not None:
            return await fg.start_id()
    return None


def _foreground_eligible(status: str) -> bool:
    """True when *status* permits the foreground last resort."""
    return status in gateway_service.FOREGROUND_ELIGIBLE


# --- make-live: switch the live gateway to another worktree ---
#
# `_restart_gateway` only bounces the live unit in place — the shipped unit
# file hardcodes WorkingDirectory/ExecStart/PATH, so there is no way to point
# the live gateway at a DIFFERENT worktree. Make-live closes that gap with a
# systemd drop-in that OVERRIDES those three fields (the main unit file is
# never edited), then a detached restart applies it.


def _in_pod() -> bool | None:
    """Whether THIS backend runs inside a pod (config home under
    ``.kirocrew-pods/<name>``) — same detection as _gateway_unit_name.

    Returns ``True`` (definitely a pod), ``False`` (definitely not), or
    ``None`` when pod status cannot be resolved (config home unresolvable).

    Cutting the real live gateway from a pod plane is refused: a pod is a
    throwaway test instance and must never repoint the operator's live
    gateway. The ambiguous ``None`` case is fail-CLOSED by the caller
    (``_make_live`` refuses with ``pod_indeterminate``) — an unresolvable
    home must NEVER be treated as "not a pod", which would let a pod cut the
    operator's live gateway."""
    try:
        from kiro_crew.config.loader import config_dir

        return config_dir().parent.name == ".kirocrew-pods"
    except Exception:  # noqa: BLE001
        return None


def _dropin_path() -> Path:
    """Absolute path of the make-live systemd drop-in for the live unit.

    Honours ``$XDG_CONFIG_HOME`` (systemd --user reads units there when set)
    and falls back to ``~/.config`` — a literal ``~/.config`` would be the
    WRONG directory on a host that sets XDG_CONFIG_HOME, and the override
    would silently never take effect."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / "systemd" / "user" / f"{_LIVE_GATEWAY_UNIT}.d" / "make-live.conf"


#: A path/value cannot be safely serialised into a service definition.
#:
#: Aliased to the adapter's exception so the systemd renderer below and the
#: launchd backend raise ONE type: ``_make_live`` catches a single class, and the
#: existing ``pytest.raises(mod._UnsafeUnitValue)`` assertions keep working.
#:
#: Raised for a value containing a newline, NUL, or any other control character.
#: Such a value would split or truncate the drop-in, and because the broken
#: override is PERSISTED, the failed cutover would then poison every subsequent
#: restart of the live unit (the restart stops the gateway but the malformed unit
#: refuses to start, and recovery restarts hit the same wall).
_UnsafeUnitValue = gateway_service._UnsafeTargetValue


# Control chars (C0 range + DEL) are unrepresentable in a single directive
# value: NUL/newline split or truncate the unit; a tab is ambiguous whitespace.
_SD_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
# A value needs double-quoting only when it carries whitespace or a systemd
# command-line / assignment metacharacter. A plain path is emitted verbatim so
# ordinary worktrees render byte-for-byte identically to before this guard.
_SD_NEEDS_QUOTE_RE = re.compile(r"""[\s"'\\$;`]""")


def _sd_value(raw: str) -> str:
    """Serialise *raw* for a systemd unit directive value.

    All three directives make-live emits — ``WorkingDirectory``, ``ExecStart``
    and ``Environment`` — undergo specifier expansion, so a literal ``%`` is
    doubled to ``%%``. Control characters are rejected outright
    (``_UnsafeUnitValue`` → ``unsafe_path``). Only when *raw* contains
    whitespace or a systemd metacharacter is it wrapped in double quotes (with
    ``\\`` and ``"`` backslash-escaped, per systemd's command-line C-style
    quoting); a clean path is returned unquoted so existing units are
    unchanged."""
    if _SD_CTRL_RE.search(raw):
        raise _UnsafeUnitValue(repr(raw))
    escaped = raw.replace("%", "%%")
    if _SD_NEEDS_QUOTE_RE.search(raw):
        inner = escaped.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{inner}"'
    return escaped


def _dropin_content(worktree: Path, kcbin: Path) -> str:
    """Render the drop-in that repoints the live unit at *worktree*.

    The lone empty ``ExecStart=`` line RESETS the unit's ExecStart before the
    replacement — systemd otherwise APPENDS, and a Type=simple service with
    two ExecStart values is a fatal unit error. ``~`` is NOT expanded inside
    ``Environment=``, so the operator bin dir is materialised to an absolute
    path here (a literal ``~/.local/bin`` would corrupt PATH).

    Every interpolated value passes through ``_sd_value`` so a worktree path
    with spaces, ``%`` specifiers, or quotes is escaped (and a control-char
    path rejected) rather than silently splitting/expanding the directive."""
    venv_bin = worktree / ".venv" / "bin"
    local_bin = Path.home() / ".local" / "bin"
    path_env = ":".join([str(venv_bin), str(local_bin), "/usr/local/bin", "/usr/bin", "/bin"])
    return (
        "[Service]\n"
        f"WorkingDirectory={_sd_value(str(worktree))}\n"
        "ExecStart=\n"
        f"ExecStart={_sd_value(str(kcbin))} gateway --no-open\n"
        f"Environment={_sd_value('PATH=' + path_env)}\n"
    )


async def _live_user_unit_status() -> str:
    """Classify the live gateway unit for make-live eligibility.

    make-live writes a ``systemctl --user`` drop-in and restarts the --user
    unit. A ``kirocrew service install`` SYSTEM unit
    (``/etc/systemd/system/kirocrew.service``) is NOT controllable that way:
    the drop-in would be written and the cutover would "succeed" while the
    detached ``--user restart`` bounces nothing (a silent false success).
    Gate on the unit actually being known to the --user manager
    (``systemctl --user cat`` rc==0, same plane ``_restart_gateway`` acts on).

    Returns:
      ``"no_systemd"``   — not Linux / systemctl absent (make-live needs --user systemd);
      ``"no_user_unit"`` — systemctl present but the live unit is not a loaded
                           --user unit (a system-unit install, or not installed);
      ``"user_unit_inactive"`` — the unit is loaded but NOT running, so it is not
                           the process serving this request: restarting it would
                           bounce an idle unit while the real gateway (foreground,
                           or a system unit) keeps serving the old code;
      ``"no_launchd"`` / ``"no_agent"`` / ``"agent_not_indirected"`` /
      ``"agent_restart_contract_outdated"`` — the launchd counterparts (see
                           ``gateway_service``);
      ``"ok"``           — the live service is known to the manager AND running,
                           so a restart actually replaces the gateway we are in.
    """
    svc = _gateway_backend()
    # No manager at all on this host: report the systemd code, which the
    # dashboard already maps, rather than inventing a third "no platform" state.
    if svc is None:
        return "no_systemd"
    status = await svc.status()
    # Loadedness alone is not drivability: a cutover that bounces a unit which is
    # not the running gateway "succeeds" while the old code keeps serving, and the
    # UI would run its restart handshake to a false completion.
    if status == "ok" and not await svc.active():
        return "user_unit_inactive"
    return status


async def _staged_cancel_available_local() -> bool:
    """GATEWAY-LOCAL probe behind :func:`_staged_cancel_available`."""
    svc = _gateway_backend()
    if svc is None:
        return True
    return (await _live_user_unit_status()) != "ok"
