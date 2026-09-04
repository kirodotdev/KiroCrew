"""Gateway restart and make-live orchestration for Dev Fleet."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.dev_fleet import gateway_service, repository, runtime
from kiro_crew.executors import subprocess_executor
from kiro_crew.instances import run_marker
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.service import live_target

# Which checkout powers the live gateway (the upstream reference showed this
# per-row as is_live; users need to see what occupies the main instance).
_LIVE_WORKTREE: str | None = None
_LIVE_CHECK_AT: float = 0.0
_LIVE_TTL = 30.0


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
    """
    pointed = live_target.read_target()
    if pointed is None:
        return None
    running = _running_checkout()
    if running is None or repository._same_path(str(pointed), str(running)):
        return None
    return str(pointed)


async def _live_worktree_path(*, fresh: bool = False) -> str | None:
    """Resolve the checkout the live gateway is RUNNING from (or None).

    ``fresh=True`` bypasses the 30s display cache -- destructive callers
    (worktree removal) must never authorize against a stale answer: the
    gateway can switch checkouts within the TTL window.
    """
    global _LIVE_WORKTREE, _LIVE_CHECK_AT
    now = time.monotonic()
    if not fresh and _LIVE_CHECK_AT and (now - _LIVE_CHECK_AT) < _LIVE_TTL:
        return _LIVE_WORKTREE
    _LIVE_CHECK_AT = now
    # The live-target pointer outranks every service-definition probe below —
    # but ONLY once the gateway is actually running it. A cutover always writes
    # the pointer, and on a host whose service cannot be driven it writes ONLY
    # the pointer, so the unit's WorkingDirectory still names the checkout the
    # gateway was installed from: reading the definition first would report that
    # stale checkout as live and leave `already_live` and `is_live` wrong.
    #
    # Honouring the pointer unconditionally is the opposite error, and the worse
    # one: between staging and the manual restart the pointer names a checkout
    # the gateway is NOT executing, so the fleet would mark it live while the old
    # image serves real data — the exact wrong conclusion this feature exists to
    # prevent. ``_running_checkout()`` is authoritative for what is executing, so
    # the pointer is only "live" when the two agree; otherwise it is staged
    # (see ``_staged_target``) and resolution falls through to the definition.
    pointed = live_target.read_target()
    if pointed is not None:
        running = _running_checkout()
        if running is None or repository._same_path(str(pointed), str(running)):
            _LIVE_WORKTREE = str(pointed)
            return _LIVE_WORKTREE
        _LIVE_WORKTREE = str(running)
        return _LIVE_WORKTREE
    if sys.platform == "darwin" and shutil.which("launchctl"):
        # launchd has no WorkingDirectory to query: the live target IS whatever
        # the agent's ProgramArguments symlink currently points at. Reading the
        # link is authoritative, needs no service query, and reflects a make-live
        # swap immediately.
        _LIVE_WORKTREE = _launchd_live_worktree()
        return _LIVE_WORKTREE
    if sys.platform != "linux" or not shutil.which("systemctl"):
        _LIVE_WORKTREE = None
        return None
    # Prefer WorkingDirectory: make-live always writes it alongside ExecStart,
    # and the baseline unit sets it too. ``--value`` prints the bare path with
    # no ``WorkingDirectory=`` prefix and, crucially, NO truncation at spaces —
    # so a checkout path containing a space resolves correctly. The old
    # ExecStart ``path=([^ ;]+)`` regex truncates at the first space, which
    # (now that make-live escapes space paths into the drop-in) would leave
    # is_live / already_live perpetually unmatched for such a worktree and
    # drive pointless repeat restarts.
    path = None
    rc, out, _err = await runtime._run_cmd(
        [
            "systemctl",
            "--user",
            "show",
            _LIVE_GATEWAY_UNIT,
            "--property=WorkingDirectory",
            "--value",
        ],
        timeout=5,
    )
    if rc == 0 and out.strip():
        path = out.strip()
    else:
        # Fallback: parse ExecStart's ``path=`` when WorkingDirectory is empty
        # (an older unit that predates the WorkingDirectory= directive).
        rc, out, _err = await runtime._run_cmd(
            ["systemctl", "--user", "show", _LIVE_GATEWAY_UNIT, "-p", "ExecStart"],
            timeout=5,
        )
        if rc == 0 and out:
            m = re.search(r"path=([^ ;]+)", out)
            if m:
                exe = Path(m.group(1))
                # <checkout>/.venv/bin/kirocrew -> <checkout>
                if ".venv" in exe.parts:
                    path = str(exe.parents[2])
    try:
        _LIVE_WORKTREE = str(Path(path).resolve()) if path else None
    except OSError:
        _LIVE_WORKTREE = None
    return _LIVE_WORKTREE


# --- gateway service detection + restart ---
_GATEWAY_SERVICE_ACTIVE: bool | None = None
_GATEWAY_SERVICE_CHECK_AT: float = 0.0
_GATEWAY_SERVICE_TTL = 30.0
_LIVE_GATEWAY_UNIT = "kirocrew-gateway.service"
# The launchd counterpart of the live systemd unit. Same agent
# `kirocrew service install` writes (kiro_crew.service.common.LAUNCHD_LABEL);
# duplicated as a literal rather than imported so this module keeps importing
# cleanly on hosts where the service package's optional deps are unavailable.
_LIVE_GATEWAY_LABEL = "dev.kirocrew.gateway"

# Single-flights the make-live cutover. Two concurrent cutovers would race on
# the shared drop-in (snapshot -> atomic-write -> daemon-reload -> systemd-run
# -> rollback): one request's failure rollback could restore/delete the OTHER
# request's successful override, restarting the gateway into the wrong
# worktree. The mutation sequence in ``_make_live`` runs under this lock; a
# second concurrent request fails fast with ``busy`` rather than queueing (a
# queued cutover could apply a stale target after the winner already restarted
# the gateway out from under us).
_MAKE_LIVE_LOCK = LoopBoundLock()

# Process-local "cutover committed" latch. ``systemd-run --collect ... restart``
# only SCHEDULES the restart and returns immediately, so ``_MAKE_LIVE_LOCK`` is
# released while the restart is still pending. Without this latch a second
# cutover could then acquire the lock and mutate the drop-in for target B while
# target A's already-scheduled restart tears this backend down mid-write —
# leaving the loaded unit and the persisted drop-in disagreeing. Once a cutover
# is successfully scheduled we set this True (BEFORE returning) and refuse every
# further request for the rest of THIS process's life with ``restart_pending``.
# It is deliberately process-local and never persisted: the fresh gateway the
# restart spawns starts with it clear. Failure paths BEFORE successful
# scheduling never set it, and ``dry_run`` never sets it.
_MAKE_LIVE_COMMITTED = False


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


async def _gateway_service_reason() -> str | None:
    """Human-readable reason the gateway service cannot be driven, or ``None``.

    Reuses the make-live eligibility codes so one probe explains both controls.
    The live-checkout hint is appended for the case that motivated this field:
    on a packaged desktop app the gateway runs from inside the bundle, so even a
    successful restart would not pick up a Pull+Build of the main checkout — and
    the previous UI said nothing at all.
    """
    if await _gateway_service_active():
        return None
    status = await _live_user_unit_status()
    reason = _make_live_status_error(status)
    if status in {"no_agent", "no_user_unit"} and await _live_worktree_path() is None:
        reason += (
            ". The running gateway does not belong to any known worktree, so "
            "restarting it would not apply a Pull+Build of the main checkout"
        )
    return reason


async def _staged_cancel_available() -> bool:
    """Whether ``_make_live``'s pointer-only cancel of a staged cutover would
    be accepted on this host.

    Mirrors the cancel branch's own precondition (``not can_restart``). The
    pointer-only cancel is deliberately limited to hosts whose service manager
    this app cannot drive: on a drivable host the stage also carries a service
    DEFINITION, so ``_make_live`` refuses the shortcut with
    ``staged_cutover_pending``. The fleet payload reports this so the dashboard
    only offers a cancel control the backend will accept — note it is NOT the
    same signal as ``_gateway_service_active()``, which also goes true for the
    foreground last resort (where ``can_restart`` stays false and the cancel
    DOES work).
    """
    svc = _gateway_backend()
    if svc is None:
        return True
    return (await _live_user_unit_status()) != "ok"


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
        # the _make_live probe at line ~4558.
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


async def _restart_gateway() -> dict:
    """Restart the gateway, preferring the service backend with foreground fallback.

    Tries the platform service manager first (systemd/launchd). When that is
    unavailable or inactive, falls through to the foreground backend — the same
    detach-and-respawn path that Make Live uses on hosts without a drivable
    service manager.

    Returns the pre-restart ``start_id`` so the frontend can poll until a
    DIFFERENT identity appears.
    """
    # Reject while a Make Live cutover is in-flight: restarting mid-staging
    # would tear the gateway down between the pointer write and the reload,
    # leaving persisted and loaded targets diverged. Acquire the lock to
    # prevent a concurrent Make Live from starting while we restart.
    global _MAKE_LIVE_COMMITTED
    if _MAKE_LIVE_COMMITTED:
        return {
            "ok": False,
            "error": "a Make Live cutover is in progress — retry after it completes",
        }
    if _MAKE_LIVE_LOCK.locked():
        return {
            "ok": False,
            "error": "a Make Live cutover is in progress — retry after it completes",
        }
    async with _MAKE_LIVE_LOCK:
        if _MAKE_LIVE_COMMITTED:
            return {
                "ok": False,
                "error": "a Make Live cutover is in progress — retry after it completes",
            }

        svc = _gateway_backend()
        service_active = False if svc is None else await svc.active()

        if service_active:
            assert svc is not None  # narrowing: service_active implies svc is not None
            start_id = await _gateway_start_id()
            ok, err = await svc.restart_detached()
            if not ok:
                return {"ok": False, "error": runtime._redact(err)}
            _MAKE_LIVE_COMMITTED = True
            return {"ok": True, "start_id": start_id}

        # Foreground fallback: hosts without a drivable service manager (e.g. AL2
        # with broken sudo, no systemd --user bus) can still restart via the
        # detach-and-respawn path — but only when not confined (status check
        # mirrors _make_live and _gateway_service_active).
        status = await _live_user_unit_status()
        if _foreground_eligible(status):
            fg = _foreground_backend()
            if fg is not None:
                fg_status = await fg.status()
                if fg_status == gateway_service.STATUS_OK:
                    start_id = await _gateway_start_id()
                    ok, err = await fg.restart_detached()
                    if not ok:
                        return {"ok": False, "error": runtime._redact(err)}
                    _MAKE_LIVE_COMMITTED = True
                    return {"ok": True, "start_id": start_id}
                # Foreground eligible but confined/broken — surface the
                # foreground's own refusal reason plus the manual remedy.
                return {
                    "ok": False,
                    "error": (
                        f"foreground gateway cannot restart ({fg_status})"
                        f" — run `{_manual_restart_command()}` to apply the build"
                    ),
                }

        # Neither the service backend nor the foreground fallback can drive the
        # restart — surface the specific reason plus the manual remedy.
        return {
            "ok": False,
            "error": (
                f"{_make_live_status_error(status)}"
                f" — run `{_manual_restart_command()}` to apply the build"
            ),
        }


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


def _staged_notice(name: str, unit_status: str) -> str:
    """Operator-facing message for a cutover that staged but could not restart.

    Leads with the remedy: the actionable command is what the operator needs
    first, and the reason is diagnostic context after it. The reason string
    already carries the "Dev Fleet cannot restart it" clause, so this must not
    restate it.
    """
    return (
        f"{name} is staged as the live target. Run "
        f"`{_manual_restart_command()}` to finish the cutover — the gateway "
        f"will come up on it. To back out instead, use the live row's "
        f"Cancel staged cutover in Dev Fleet. It was not automatic because "
        f"{_make_live_status_error(unit_status)}."
    )


def _make_live_status_error(code: str) -> str:
    """Operator-facing message for a non-``ok`` service status.

    Every message names the concrete remedy: an unmanageable service is the one
    make-live failure a user cannot diagnose from the UI alone, and the macOS
    variants are only reachable on a host where the previous behaviour was to
    hide the control entirely.
    """
    return {
        "no_systemd": (
            "the gateway does not run as a systemd --user service, so Dev Fleet "
            "cannot restart it for you"
        ),
        "no_user_unit": (
            f"the live gateway is not running as the user service "
            f"{_LIVE_GATEWAY_UNIT} — Dev Fleet cannot restart it for you (a "
            "`kirocrew service install` system unit needs root to bounce)"
        ),
        "user_unit_inactive": (
            f"the user service {_LIVE_GATEWAY_UNIT} exists but is not running, so "
            "this gateway is not it — restarting that unit would leave the "
            "gateway you are talking to untouched"
        ),
        "no_launchd": (
            "the gateway does not run as a launchd user agent, so Dev Fleet "
            "cannot restart it for you"
        ),
        "no_agent": (
            f"the live gateway is not running as the launchd agent "
            f"{_LIVE_GATEWAY_LABEL} — it was most likely started by the "
            "packaged app or from a terminal, so Dev Fleet cannot restart it "
            "for you"
        ),
        "agent_not_indirected": (
            f"the launchd agent {_LIVE_GATEWAY_LABEL} does not run through the "
            "live-gateway launcher, so Dev Fleet does not treat it as one it "
            "can safely bounce. Re-run `kirocrew service install` to refresh "
            "the agent definition"
        ),
        "agent_restart_contract_outdated": (
            f"the launchd agent {_LIVE_GATEWAY_LABEL} lacks the bounded graceful "
            "restart contract required by Dev Fleet. Re-run "
            "`kirocrew service install` to refresh the agent definition"
        ),
        "live_program_missing": (
            f"the launchd agent {_LIVE_GATEWAY_LABEL} is loaded but its "
            "live-gateway launcher is missing (deleted application-support "
            "directory?), so it has nothing to execute. Make live onto a "
            "worktree to rewrite it, or start a gateway from your source "
            "checkout — either restores the launcher without touching the "
            "agent definition, whereas kirocrew service install would rewrite "
            "the whole plist and discard any environment you added to it"
        ),
    }.get(code, f"the live gateway cannot be repointed ({code})")


def _manual_restart_command() -> str:
    """The command an operator runs to finish a staged cutover themselves.

    Always the service-aware ``kirocrew restart``: it resolves whatever manager
    owns the gateway (or a foreground process) at run time. Naming a specific
    ``systemctl`` invocation here would guess, and guessing wrong hands the
    operator a command that fails while the staged pointer stays unapplied — a
    Linux host with ``systemctl`` present may still be running the gateway from a
    terminal with no unit to bounce.
    """
    return "kirocrew restart"


def _make_live_plan(
    worktree: Path,
    kcbin: Path,
    *,
    svc: "gateway_service.GatewayServiceBackend | None",
    foreground: "gateway_service.ForegroundBackend | None" = None,
) -> dict:
    """Describe — without mutating anything — what making *worktree* live does.

    Validates the target the same way the real cutover does, so a dry run
    reports an unusable worktree instead of promising a cutover that would then
    be refused. When the service is drivable the backend's own plan is folded in,
    because the cutover restages that definition too. *foreground* is the
    last-resort restart that will be ATTEMPTED when no manager is drivable
    (see ``_make_live``): a dry run must report that restart as automatic, or
    the preview would promise a manual step the real call then performs itself.
    """
    live_target.validate(str(worktree))
    plan: dict = {
        "mechanism": "live-target pointer",
        "pointer_path": str(live_target.pointer_path()),
        "exec": str(kcbin),
        "restart": "automatic" if (svc is not None or foreground is not None) else "manual",
    }
    if svc is not None:
        plan.update(svc.plan(worktree, kcbin))
    elif foreground is not None:
        plan.update(foreground.plan(worktree, kcbin))
    else:
        plan["manual_restart"] = _manual_restart_command()
    return plan


async def _make_live(path: str, dry_run: bool = False, expected_staged: str | None = None) -> dict:
    """Repoint the live gateway at *path* by staging the live-target pointer.

    ``expected_staged`` binds a CANCEL to the state the operator confirmed:
    when set, the request proceeds only while the staged target still
    resolves to that path (checked at entry and re-checked under the
    single-flight lock) AND *path* still resolves to the running checkout,
    refusing with ``stage_changed`` otherwise — without both bindings, a
    cancel confirmed against one stage/live pair could silently discard a
    different stage, or fall through to the cutover path and restart the
    gateway into a checkout that is no longer live.

    Validation order (all enforced for ``dry_run`` too): the path is a known,
    existing worktree (``unknown_path`` / ``missing_path``); NOT inside a pod,
    fail-CLOSED on indeterminate pod status (``pod`` / ``pod_indeterminate``);
    not already live (``already_live``); the worktree has its own executable
    ``.venv/bin/kirocrew`` (else Provision -> ``missing_venv`` when absent,
    ``venv_not_executable`` when present but not +x) and a built SPA
    ``dist/index.html`` (else Pull+Build -> ``missing_dist``).

    A real cutover writes the pointer, then bounces the gateway through the
    service manager when there is one we can drive (DETACHED, so it survives our
    own death — mirroring ``_restart_gateway``). When there is not, the pointer
    still stands and the response carries ``staged_only`` plus the one command
    that finishes it: the gateway reads the pointer on ITS next start, whoever
    performs it. Staging deliberately does NOT require a drivable service, which
    is what keeps a ``kirocrew service install`` host (a SYSTEM unit, needing
    root to bounce) able to cut over at all.
    """
    global _MAKE_LIVE_COMMITTED, _LIVE_WORKTREE, _LIVE_CHECK_AT
    # A cutover already scheduled in THIS process. systemd-run has returned but
    # the restart is still pending, so refuse up-front (before any validation or
    # dry_run plan) — any further mutation would race the pending restart.
    if _MAKE_LIVE_COMMITTED:
        return {
            "ok": False,
            "code": "restart_pending",
            "error": (
                "a cutover has been scheduled; the gateway is restarting — "
                "retry after it comes back"
            ),
        }
    target, err = await repository._find_worktree_by_path(path)
    if target is None:
        return {"ok": False, "code": "unknown_path", "error": err}
    real = Path(target["path"])
    if not real.exists():
        return {
            "ok": False,
            "code": "missing_path",
            "error": f"worktree path no longer exists: {real}",
        }

    pod = _in_pod()
    if pod is None:
        return {
            "ok": False,
            "code": "pod_indeterminate",
            "error": (
                "cannot determine whether this backend runs inside a pod (config "
                "home unresolvable) — refusing make-live to avoid repointing the "
                "live gateway from an unattributable plane"
            ),
        }
    if pod:
        return {
            "ok": False,
            "code": "pod",
            "error": (
                "refusing make-live from inside a pod — a pod is a throwaway test "
                "instance and must never repoint the real live gateway "
                "(run this from the live dashboard)"
            ),
        }

    # A request carrying expected_staged is a CANCEL of that exact stage and
    # nothing else. Validated HERE, before any branching: if the named stage
    # completed or was re-pointed while the confirm dialog sat open, the
    # request must not fall through to the full-cutover path below — that
    # would restart the gateway back into the requested checkout, turning a
    # stale "cancel" into the destructive opposite of what the operator asked.
    # Re-checked under the single-flight lock before the pointer write.
    if expected_staged is not None:
        pending_entry = _staged_target()
        if pending_entry is None or not repository._same_path(expected_staged, pending_entry):
            now_desc = (
                f"{Path(pending_entry).name} is staged now"
                if pending_entry is not None
                else "nothing is staged now"
            )
            return {
                "ok": False,
                "code": "stage_changed",
                "error": (
                    "the staged cutover changed while you were confirming: "
                    f"{now_desc}, not {Path(expected_staged).name} — refresh "
                    "the fleet and retry"
                ),
            }

    # The live target is a POINTER the gateway resolves at startup, not an edit
    # to this host's service definition — so staging never needs the service
    # manager. Restarting still does, and that is the one thing a `kirocrew
    # service install` SYSTEM unit cannot give us without root: the cutover is
    # staged either way, and when we cannot bounce the gateway ourselves we hand
    # the operator the one command that finishes it. Refusing here instead would
    # make the whole feature unreachable on the most common Linux install.
    svc = _gateway_backend()
    unit_status = await _live_user_unit_status()
    can_restart = svc is not None and unit_status == "ok"
    # LAST RESORT (strictly systemd > launchd > foreground): when no manager is
    # drivable at all, a detached `kirocrew restart` can still finish the
    # cutover (see gateway_service.ForegroundBackend). Probed ONCE per request,
    # mirroring can_restart, so the plan and the act cannot disagree. Only the
    # FOREGROUND_ELIGIBLE codes qualify — a mis-set-up manager keeps its named
    # remedy instead of being bounced behind its back.
    foreground: "gateway_service.ForegroundBackend | None" = None
    if not can_restart and _foreground_eligible(unit_status):
        fg = _foreground_backend()
        if fg is not None and await fg.status() == gateway_service.STATUS_OK:
            foreground = fg

    live = await _live_worktree_path()
    same_as_running = live is not None and repository._same_path(str(real), live)
    if expected_staged is not None and not same_as_running:
        # A request carrying expected_staged is a CANCEL: it re-pins the
        # checkout the operator saw as live. If the live checkout moved since
        # the dialog (a cutover landed and re-staged in between), the request
        # names a checkout that is no longer running — falling through to the
        # cutover path below would restart the gateway into it, the
        # destructive opposite of a cancel. Refuse instead.
        live_name = Path(live).name if live else "an unknown checkout"
        return {
            "ok": False,
            "code": "stage_changed",
            "error": (
                "the live checkout changed while you were confirming: "
                f"{live_name} is running now, not {real.name} — refresh the "
                "fleet and retry"
            ),
        }
    if same_as_running and _staged_target() is None:
        # Nothing staged: pointing at the checkout already running is a no-op on
        # EVERY host. This guard sits before the cancel below so that a drivable
        # host cannot turn a harmless repeat click into a real gateway restart by
        # falling through to the cutover path.
        return {
            "ok": False,
            "code": "already_live",
            "error": f"{real.name} is already the live gateway",
        }
    if same_as_running and not can_restart:
        # Pointing at the checkout already running is normally a no-op — EXCEPT
        # while a cutover is staged, where it is the operator cancelling it. The
        # pointer names a different checkout than the running image, so re-pinning
        # the running one is exactly "stay on what is running", and it is the only
        # cancel a non-drivable host can offer: without this the operator's only
        # routes are to complete the cutover into the wrong code and reverse it
        # (two manual restarts) or to hand-delete a keystone-fenced file the
        # product never names.
        #
        # Deliberately limited to hosts this app cannot drive. A drivable host
        # also stages a service DEFINITION naming the staged checkout, and this
        # shortcut only touches the pointer — so the definition would keep naming
        # a checkout nobody intends to run. Once that checkout is pruned the unit
        # fails to start before it ever reads the pointer, turning a recoverable
        # mis-stage into a gateway that will not boot. A drivable host therefore
        # falls through to the full cutover below, which restages the definition
        # and the pointer together and restarts.
        pending_target = _staged_target()
        if pending_target is None:
            # Defensive re-read: the check above and this one straddle no await,
            # but keeping it means the cancel never builds a plan around a stage
            # that has since disappeared.
            return {
                "ok": False,
                "code": "already_live",
                "error": f"{real.name} is already the live gateway",
            }
        cancel_plan = {
            "action": "cancel_staged_cutover",
            "staged_target": pending_target,
            "keeps_live_target": str(real),
            "pointer_path": str(live_target.pointer_path()),
            "restart": "not needed",
        }
        # Deleting the pointer IS a mutation, so it owes the same two duties as
        # the cutover below: never act under ``dry_run``, and never touch the
        # pointer outside the single-flight lock.
        if dry_run:
            return {"ok": True, "dry_run": True, "plan": cancel_plan}
        if _MAKE_LIVE_LOCK.locked():
            return {
                "ok": False,
                "code": "busy",
                "error": ("another make-live cutover is in progress"),
            }
        async with _MAKE_LIVE_LOCK:
            if _MAKE_LIVE_COMMITTED:
                return {
                    "ok": False,
                    "code": "restart_pending",
                    "error": (
                        "a cutover has been scheduled; the gateway is restarting — "
                        "retry after it comes back"
                    ),
                }
            # Re-read under the lock: the awaits above mean the stage may have
            # been completed or re-pointed since the entry check, and cancelling
            # a stage that no longer exists would delete a pointer someone else
            # just wrote.
            pending_now = _staged_target()
            if pending_now is None:
                return {
                    "ok": False,
                    "code": "already_live",
                    "error": f"{real.name} is already the live gateway",
                }
            if expected_staged is not None and not repository._same_path(
                expected_staged, pending_now
            ):
                # The stage moved between the entry check and the lock: this
                # cancel was confirmed against a different target, so acting
                # would discard a stage the operator never saw.
                return {
                    "ok": False,
                    "code": "stage_changed",
                    "error": (
                        "the staged cutover changed while you were confirming: "
                        f"{Path(pending_now).name} is staged now, not "
                        f"{Path(expected_staged).name} — refresh the fleet and retry"
                    ),
                }
            # Re-pin the RUNNING checkout rather than deleting the pointer.
            # Deleting only means "stay here" when the running image is the
            # installed build; if this checkout was itself selected by an earlier
            # cutover, the pointer is the only record of that choice, so removing
            # it would silently demote the operator back to the installed build
            # on the next restart — the opposite of the cancel they asked for.
            # Writing is idempotent when the pointer already named it.
            loop = asyncio.get_running_loop()
            try:
                prior_pointer = await loop.run_in_executor(
                    subprocess_executor(), live_target.snapshot
                )
            except (OSError, ValueError) as exc:
                return {
                    "ok": False,
                    "code": "write_failed",
                    "error": (
                        "refusing to cancel the staged cutover: the staged pointer "
                        "exists but could not be read, so a failed cancel could not "
                        f"be rolled back: {runtime._redact(str(exc))}"
                    ),
                }
            try:
                await loop.run_in_executor(subprocess_executor(), live_target.write_target, real)
            except (live_target.InvalidTarget, OSError) as exc:
                # InvalidTarget refuses before anything is written. OSError can
                # arrive AFTER the pointer has been replaced, because
                # write_target re-applies the owner-only mode as its last step —
                # so a failure there would otherwise leave a code-execution input
                # in place with inherited permissions while this call reported
                # failure. Roll the pointer back so the cancel is all-or-nothing,
                # and only when there was one: restore(None) DELETES, which is
                # the demotion this branch exists to avoid.
                rolled_back = True
                if prior_pointer is not None:
                    rolled_back = await loop.run_in_executor(
                        subprocess_executor(), live_target.restore, prior_pointer
                    )
                detail = (
                    ""
                    if rolled_back
                    else (
                        " The rollback also failed, so the pointer may name the "
                        "running checkout without owner-only permissions — check it "
                        "before the next restart."
                    )
                )
                return {
                    "ok": False,
                    "code": "write_failed",
                    "error": (
                        "refusing to cancel the staged cutover: the running "
                        "checkout could not be re-pinned as the live target: "
                        f"{runtime._redact(str(exc))}.{detail}"
                    ),
                }
            _LIVE_WORKTREE = None
            _LIVE_CHECK_AT = 0.0
            return {
                "ok": True,
                "cancelled": True,
                "target": str(real),
                "plan": cancel_plan,
                "notice": (
                    f"Staged cutover cancelled. {real.name} stays the live "
                    f"target and no restart is needed."
                ),
            }
    if same_as_running:
        # Drivable host with a stage pending. The pointer-only cancel above is
        # unsafe here (it would leave the service definition naming the staged
        # checkout), but falling through to the full cutover would bounce a live
        # gateway carrying real sessions in response to a request that reads as
        # "keep running what is already running". Refuse and name both real
        # exits instead: surprising an operator in the destructive direction is
        # worse than doing nothing.
        pending = _staged_target()
        pending_name = Path(pending).name if pending else "another checkout"
        return {
            "ok": False,
            "code": "staged_cutover_pending",
            "error": (
                f"a cutover to {pending_name} is already staged. Dev Fleet can "
                f"restart this host, so cancelling by re-pointing here would leave "
                f"the service definition naming {pending_name}. Make {pending_name} "
                f"live to complete the cutover, or restart the gateway to apply it."
            ),
        }

    kcbin = real / ".venv" / "bin" / "kirocrew"
    dist_index = real / "src" / "kiro_crew" / "static" / "dist" / "index.html"

    def _validate_artifacts_sync() -> tuple[str, str] | None:
        """Check the CLI binary and built dist on the executor thread.

        Returns ``(code, error)`` when a required artifact is absent or
        non-executable, ``None`` when both are present and the binary is
        executable.  Running off the event loop prevents a slow or
        network-backed filesystem from stalling all gateway requests.
        """
        if not kcbin.is_file():
            return (
                "missing_venv",
                (
                    f"{real.name} has no .venv/bin/kirocrew — Provision it first "
                    "(row menu \u2192 Provision) before making it live"
                ),
            )
        # A present-but-non-executable binary is worse than a missing one: the
        # drop-in gets written and the old gateway is stopped, but the
        # replacement can never start (systemd ExecStart requires +x) — leaving
        # NO gateway running.  Gate on the exec bit with a DISTINCT, actionable
        # code.
        if not os.access(kcbin, os.X_OK):
            return (
                "venv_not_executable",
                (
                    f"{real.name} has a non-executable .venv/bin/kirocrew — run "
                    "`chmod +x` on it or re-Provision the worktree before making "
                    "it live (a non-executable binary stops the live gateway but "
                    "cannot start the replacement, leaving no gateway running)"
                ),
            )
        if not dist_index.is_file():
            return (
                "missing_dist",
                (
                    f"{real.name} has no built dashboard "
                    "(src/kiro_crew/static/dist/index.html) — run Pull+Build "
                    "first; cutover without a built dist serves a broken dashboard"
                ),
            )
        return None

    # Early probe: surface an obvious missing-artifact error before reaching
    # the plan or the lock.  Not authoritative — a concurrent provision or
    # rebuild can change these artifacts between this check and the cutover
    # lock below.  The authoritative re-validation happens inside the lock.
    _loop = asyncio.get_running_loop()
    artifact_err = await _loop.run_in_executor(subprocess_executor(), _validate_artifacts_sync)
    if artifact_err is not None:
        code, msg = artifact_err
        return {"ok": False, "code": code, "error": msg}

    try:
        plan = _make_live_plan(real, kcbin, svc=svc if can_restart else None, foreground=foreground)
    except live_target.InvalidTarget as exc:
        return {
            "ok": False,
            "code": "unsafe_path",
            "error": (
                "refusing make-live: the worktree path cannot be used as a live "
                f"target: {runtime._redact(str(exc))}"
            ),
        }
    except gateway_service._UnsafeTargetValue as exc:
        return {
            "ok": False,
            "code": "unsafe_path",
            "error": (
                "refusing make-live: the worktree path is not safely representable "
                "in a service definition (contains control characters): "
                f"{runtime._redact(str(exc))}"
            ),
        }
    plan["target"] = str(real)
    if dry_run:
        return {"ok": True, "dry_run": True, "plan": plan}

    # Serialize the mutation sequence: two concurrent cutovers racing on the
    # shared drop-in (snapshot -> write -> reload -> restart -> rollback) could
    # have one request's rollback restore/delete the OTHER's successful
    # override, restarting into the wrong worktree. Fail fast with ``busy`` on
    # contention rather than queueing — a queued cutover would apply a stale
    # target after the winner already restarted the gateway. The check and the
    # acquire are atomic here (no ``await`` between them on the single-threaded
    # event loop), so the busy response cannot itself race the lock.
    if _MAKE_LIVE_LOCK.locked():
        return {"ok": False, "code": "busy", "error": ("another make-live cutover is in progress")}
    async with _MAKE_LIVE_LOCK:
        # Re-check the committed latch now that we hold the lock. A request
        # that passed the entry check just before the WINNING cutover latched
        # (the entry check and the lock acquire are separated by awaits) would
        # otherwise fall through here and mutate the drop-in a second time while
        # the winner's restart is already tearing us down.
        if _MAKE_LIVE_COMMITTED:
            return {
                "ok": False,
                "code": "restart_pending",
                "error": (
                    "a cutover has been scheduled; the gateway is restarting — "
                    "retry after it comes back"
                ),
            }
        # Re-validate artifacts inside the lock: a concurrent provision or
        # rebuild may have changed the binary or dist between the early probe
        # above and now.  The cutover commits the exact state on disk at this
        # moment, so these are the artifacts it actually stages.
        artifact_err = await _loop.run_in_executor(subprocess_executor(), _validate_artifacts_sync)
        if artifact_err is not None:
            code, msg = artifact_err
            return {"ok": False, "code": code, "error": msg}
        # Snapshot the prior live target BEFORE staging so a failed cutover can
        # be rolled back — a persisted pointer would otherwise silently activate
        # on the NEXT unrelated restart. Staging itself is atomic (temp file +
        # os.replace), so a partial write can never leave a truncated pointer
        # either.
        #
        # An UNREADABLE (as opposed to absent) prior pointer aborts here, before
        # anything is staged: restore interprets None as "there was nothing
        # here" and DELETES the pointer, so continuing would let a failed
        # restart destroy a live target we merely could not read.
        try:
            prior_content = live_target.snapshot()
        except (OSError, ValueError) as exc:
            # ValueError covers an undecodable pointer: it exists, so rollback
            # cannot treat it as absent (that DELETES it), and the cutover is
            # refused rather than made unreversible.
            return {
                "ok": False,
                "code": "write_failed",
                "error": (
                    "refusing make-live: the current live target exists but "
                    f"could not be read, so a failed cutover could not be rolled "
                    f"back: {runtime._redact(str(exc))}"
                ),
            }
        # A drivable service may ALSO carry staging from an earlier cutover whose
        # definition names a worktree directly. Leaving that definition pinned to
        # a stale checkout is a live landmine: once that worktree is pruned the
        # unit's ExecStart binary is gone, the service fails EXEC on its next
        # start, and the pointer is never even read — no gateway comes up. So the
        # definition is restaged alongside the pointer whenever we can drive it,
        # keeping the two in agreement and the ExecStart binary always present.
        prior_definition: str | None = None
        if can_restart:
            assert svc is not None
            try:
                prior_definition = svc.snapshot()
            except OSError as exc:
                return {
                    "ok": False,
                    "code": "write_failed",
                    "error": (
                        "refusing make-live: the current service definition exists "
                        "but could not be read, so a failed cutover could not be "
                        f"rolled back: {runtime._redact(str(exc))}"
                    ),
                }

        def _unwind_sync() -> bool:
            """Restore both staged surfaces. False when either did not land."""
            ok = live_target.restore(prior_content)
            if can_restart and svc is not None:
                ok = svc.rollback(prior_definition) and ok
            return ok

        async def _unwind() -> bool:
            # Both halves block: restore() ends in restrict_to_owner, which
            # rewrites a DACL on Windows, and svc.rollback() rewrites the service
            # definition. Offload them for the same reason the write below is
            # offloaded — an unwind must not stall every other gateway request for
            # the duration of blocking filesystem work.
            return await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), _unwind_sync
            )

        try:
            # write_target ends in restrict_to_owner, which rewrites a DACL
            # on Windows. Run it off the loop so a cutover cannot stall every
            # other gateway request for the duration of that subprocess.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(subprocess_executor(), live_target.write_target, real)
        except live_target.InvalidTarget as exc:
            return {"ok": False, "code": "unsafe_path", "error": runtime._redact(str(exc))}
        except OSError as exc:
            return {
                "ok": False,
                "code": "write_failed",
                "rolled_back": await _unwind(),
                "error": runtime._redact(str(exc)),
            }

        # Nothing MANAGED bounces the gateway on this host. When the foreground
        # last resort is usable it finishes the cutover below; otherwise the
        # cutover is STAGED and the operator finishes it — reported as a success
        # with the exact command, not a failure: the pointer is written and
        # correct, and the next start of the gateway — however it happens —
        # comes up on the new target. The staged-only outcome is deliberately
        # NOT latched as committed: no restart is pending, so a subsequent
        # cutover to a different worktree must stay allowed.
        if not can_restart:
            _LIVE_WORKTREE = None
            _LIVE_CHECK_AT = 0.0
            if foreground is not None:
                # Capture identity BEFORE establishing the restart, mirroring
                # the drivable path: the detached bounce can tear this process
                # down at any moment after the spawn.
                start_id = await foreground.start_id()
                restarted, fg_err = await foreground.restart_detached()
                if restarted:
                    # A restart IS pending now — latch exactly as the drivable
                    # path does, so no further cutover mutates the pointer
                    # while the detached `kirocrew restart` is acting on it.
                    _MAKE_LIVE_COMMITTED = True
                    return {
                        "ok": True,
                        "cutover": True,
                        "target": str(real),
                        "plan": plan,
                        "start_id": start_id,
                    }
                # FAIL SAFE: the spawn was never established, so nothing has
                # been signalled and the gateway is untouched. The pointer
                # stays staged (it is written and correct) and the operator
                # gets the exact status-quo advisory. Rolling the pointer back
                # here would be strictly worse: it would turn "finish with one
                # command" into "start over".
                runtime.logger.warning(
                    "foreground restart could not be established (%s); "
                    "falling back to the manual-restart advisory",
                    fg_err,
                )
                plan = {**plan, "restart": "manual", "manual_restart": _manual_restart_command()}
            return {
                "ok": True,
                "cutover": True,
                "staged_only": True,
                "target": str(real),
                "plan": plan,
                "manual_restart": _manual_restart_command(),
                "notice": _staged_notice(real.name, unit_status),
            }
        assert svc is not None  # can_restart implies a backend

        staged, code, err = await svc.stage(real, kcbin)
        if not staged:
            rolled_back = await _unwind()
            # Re-read definitions so the loaded config matches the restored disk
            # state rather than the rejected override.
            await svc.reload()
            return {
                "ok": False,
                "code": code,
                "rolled_back": rolled_back,
                "error": runtime._redact(err),
            }

        # The restart tears down THIS backend with the gateway, so it is handed
        # to the service manager to perform (systemd-run on Linux, launchd's
        # stop/relaunch transaction on macOS) so it survives our own death. Capture the
        # pre-restart identity FIRST so the dashboard reuses the same handshake
        # it uses for restart-gateway (wait for a DIFFERENT start id, not "a 200
        # came back") -- a cutover is just a restart into different code, so it
        # has the identical early-200 hazard. None-safe (see _gateway_start_id).
        start_id = await _gateway_start_id()
        restarted, err = await svc.restart_detached()
        if not restarted:
            rolled_back = await _unwind()
            await svc.reload()
            return {
                "ok": False,
                "code": "restart_failed",
                "rolled_back": rolled_back,
                "error": runtime._redact(err),
            }

        # COMMITTED: the restart is scheduled (the call returns before it
        # lands). Latch process-locally BEFORE returning so no further cutover
        # can mutate the live target while the restart is pending — the fresh
        # process the restart spawns starts with this clear.
        _MAKE_LIVE_COMMITTED = True

        # Invalidate the live-worktree cache so the next fleet poll re-resolves
        # the live checkout.
        _LIVE_WORKTREE = None
        _LIVE_CHECK_AT = 0.0

        return {
            "ok": True,
            "cutover": True,
            "target": str(real),
            "plan": plan,
            "start_id": start_id,
        }


__all__ = (
    "_GATEWAY_SERVICE_ACTIVE",
    "_GATEWAY_SERVICE_CHECK_AT",
    "_GATEWAY_SERVICE_TTL",
    "_LIVE_CHECK_AT",
    "_LIVE_GATEWAY_LABEL",
    "_LIVE_GATEWAY_UNIT",
    "_LIVE_TTL",
    "_LIVE_WORKTREE",
    "_MAKE_LIVE_COMMITTED",
    "_MAKE_LIVE_LOCK",
    "_SD_CTRL_RE",
    "_SD_NEEDS_QUOTE_RE",
    "_UnsafeUnitValue",
    "_dropin_content",
    "_dropin_path",
    "_foreground_backend",
    "_foreground_eligible",
    "_gateway_backend",
    "_gateway_label",
    "_gateway_service_active",
    "_gateway_service_reason",
    "_gateway_start_id",
    "_gateway_unit_name",
    "_in_pod",
    "_launchd_live_worktree",
    "_live_user_unit_status",
    "_live_worktree_path",
    "_make_live",
    "_make_live_plan",
    "_make_live_status_error",
    "_manual_restart_command",
    "_own_checkout_path",
    "_restart_gateway",
    "_running_checkout",
    "_sd_value",
    "_staged_cancel_available",
    "_staged_notice",
    "_staged_target",
)
