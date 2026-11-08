"""Per-principal containment slices, the hard-limit gate and the containment report.

Hierarchy, reusing the existing sandbox machinery (``sandbox_cgroup``,
``sandbox_slice``) rather than adding a second one::

    kirocrew-agents.slice                         aggregate memory/pids ceiling (existing)
      kirocrew-agents-<instance>.slice            this Crew; CPUQuota added here
        kirocrew-agents-<instance>-p<hash>.slice  one person: CPUQuota, MemoryMax, TasksMax
          kirocrew-rt-<token>.scope               one runtime spawn (existing per-scope limits)

systemd nests slices by the dashes in their names, so the per-principal slice
is spelled as a child of the instance slice. ``<hash>`` is 12 hex characters of
``sha256(principal id)``: no dashes (a dash would add a level) and no login.

What this guarantees, stated as the capability payload states it: the kernel
enforces the CPU, memory and pids ceilings against cooperative and buggy
workloads, aggregated across every scope a principal's work starts. It does
NOT resist an adversarial workload: every runtime runs as the gateway's uid,
which owns the delegated subtree and can reach the user bus, so a workload can
raise its own limits or start a scope outside the slice. ``level`` is therefore
``best_effort`` whenever enforcement is available, and never ``hard``. IO is not
delegated to user slices on the measured profile and is reported unavailable.
Disk is application accounting (``accounting``), not a filesystem quota.

``hard_limits`` mode turns a missing prerequisite (cgroup v2, trusted
``systemd-run``/``systemctl``, a reachable user bus, memory and pids delegation)
into an admission refusal with a machine-readable reason, using the same
``_probe_cgroup_scope`` check the per-scope wrapper uses. Without it the gate
admits work and reports the actual guarantee.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import logging
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Iterator, TypeVar

from kiro_crew.config.sections_shared_crew import EffectiveLimits
from kiro_crew.shared_crew.contracts import CapabilityState, EnforcementLevel

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Machine-readable reasons a containment prerequisite is missing.
REASON_NOT_LINUX = "not_linux"
REASON_NO_SYSTEMD_RUN = "systemd_run_untrusted"
REASON_NO_SYSTEMCTL = "systemctl_untrusted"
REASON_NO_USER_SESSION = "no_user_session"
REASON_NO_USER_BUS = "user_bus_unreachable"
REASON_NOT_CGROUP_V2 = "not_cgroup_v2"
REASON_CONTROLLERS = "controllers_not_delegated"
REASON_CONTROLLERS_UNREADABLE = "controllers_unreadable"
REASON_SLICE_FAILED = "principal_slice_failed"
REASON_UNKNOWN = "containment_probe_failed"
#: Hard-limit mode is on but fair admission, which applies the limits, is off.
REASON_ADMISSION_OFF = "admission_disabled"

_PROBE_REASON_PREFIXES: tuple[tuple[str, str], ...] = (
    ("not Linux", REASON_NOT_LINUX),
    ("systemd-run", REASON_NO_SYSTEMD_RUN),
    ("no XDG_RUNTIME_DIR", REASON_NO_USER_SESSION),
    ("user bus unreachable", REASON_NO_USER_BUS),
    ("not a cgroup v2", REASON_NOT_CGROUP_V2),
    ("cannot read /proc/self/cgroup", REASON_NOT_CGROUP_V2),
    ("controllers not delegated", REASON_CONTROLLERS),
    ("cannot read delegated controllers", REASON_CONTROLLERS_UNREADABLE),
)


def classify_probe_reason(reason: str) -> str:
    """Map ``_probe_cgroup_scope``'s prose reason onto a stable machine code."""
    for prefix, code in _PROBE_REASON_PREFIXES:
        if reason.startswith(prefix):
            return code
    return REASON_UNKNOWN


@dataclass(frozen=True)
class SliceLimits:
    cpu_quota_percent: int = 0
    memory_max_mb: int = 0
    tasks_max: int = 0

    def properties(self, *, cpu_delegated: bool) -> list[str]:
        props: list[str] = []
        if self.cpu_quota_percent > 0 and cpu_delegated:
            props.append(f"CPUQuota={self.cpu_quota_percent}%")
        if self.memory_max_mb > 0:
            props += [f"MemoryMax={self.memory_max_mb}M", "MemorySwapMax=0"]
        if self.tasks_max > 0:
            props.append(f"TasksMax={self.tasks_max}")
        return props


@dataclass(frozen=True)
class ContainmentReport:
    """What this host enforces for per-principal containment, right now."""

    platform: str
    available: bool
    reason: str
    cpu: bool
    memory: bool
    pids: bool
    io: bool = False
    disk: str = "app_accounting"
    level: EnforcementLevel = EnforcementLevel.NONE
    hard_limits: bool = False
    #: Whether hard mode's prerequisites all hold. Meaningful only with hard_limits.
    hard_ready: bool = False
    #: Present: this host cannot resist a workload that targets its own containment.
    adversarial_escape_closed: bool = False
    slices_enabled: bool = False
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def refusal_reason(self) -> str | None:
        """The machine reason hard mode refuses with, or None when it admits."""
        if not self.hard_limits:
            return None
        if self.hard_ready:
            return None
        return self.reason or REASON_UNKNOWN

    def capability_state(self) -> CapabilityState:
        if not self.available:
            return CapabilityState.UNAVAILABLE
        if not (self.cpu and self.memory and self.pids) or not self.slices_enabled:
            return CapabilityState.DEGRADED
        return CapabilityState.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "available": self.available,
            "state": self.capability_state().value,
            "level": self.level.value,
            "reason": self.reason,
            "controls": {
                "cpu": "enforced" if self.cpu else "unavailable",
                "memory": "enforced" if self.memory else "unavailable",
                "pids": "enforced" if self.pids else "unavailable",
                "io": "enforced" if self.io else "unavailable",
                "disk": self.disk,
            },
            "per_principal_slices": self.slices_enabled,
            "hard_limits": self.hard_limits,
            "hard_ready": self.hard_ready,
            "adversarial_escape_closed": self.adversarial_escape_closed,
            "detail": self.detail,
        }


def _delegated_controllers() -> set[str] | None:
    from kiro_crew import sandbox_cgroup

    try:
        with open(sandbox_cgroup._user_slice_controllers_path(), encoding="utf-8") as fh:
            return set(fh.read().split())
    except OSError:
        return None


def failed_report(*, hard_limits: bool, detail: str) -> ContainmentReport:
    """The report for a probe that raised: nothing is proven, so hard mode refuses.

    ``hard_ready`` stays false, so with ``hard_limits`` on :attr:`refusal_reason`
    is :data:`REASON_UNKNOWN` and every admission and runtime start is refused.
    """
    return ContainmentReport(
        platform=sys.platform,
        available=False,
        reason=REASON_UNKNOWN,
        cpu=False,
        memory=False,
        pids=False,
        hard_limits=hard_limits,
        detail=detail[:200],
    )


def admission_off_report() -> ContainmentReport:
    """Hard-limit mode with fair admission off: nothing applies the limits, so refuse.

    Published so ``runtime_start``'s backstop refuses every runtime start until
    ``admission_enabled`` is turned on or ``hard_limits`` off.
    """
    return ContainmentReport(
        platform=sys.platform,
        available=False,
        reason=REASON_ADMISSION_OFF,
        cpu=False,
        memory=False,
        pids=False,
        hard_limits=True,
        detail="hard_limits requires admission_enabled; no runtime starts until both are on",
    )


def probe_containment(*, hard_limits: bool, slices_enabled: bool) -> ContainmentReport:
    """Probe this host. Reads a handful of files and dials the user bus; call off-loop."""
    from kiro_crew import platform_compat, sandbox_cgroup

    platform = sys.platform
    if platform != "linux":
        return ContainmentReport(
            platform=platform,
            available=False,
            reason=REASON_NOT_LINUX,
            cpu=False,
            memory=False,
            pids=False,
            hard_limits=hard_limits,
            detail="no cgroups on this platform; per-person limits are admission counts only",
        )
    available, prose = sandbox_cgroup._probe_cgroup_scope()
    controllers = _delegated_controllers() or set()
    systemctl_ok = platform_compat.trusted_system_bin_quiet("systemctl") is not None
    reason = "" if available else classify_probe_reason(prose)
    if available and not systemctl_ok:
        reason = REASON_NO_SYSTEMCTL
    if available and systemctl_ok and not slices_enabled:
        reason = REASON_SLICE_FAILED
    usable = available and systemctl_ok and slices_enabled
    return ContainmentReport(
        platform=platform,
        available=usable,
        reason=reason,
        cpu=usable and "cpu" in controllers,
        memory=usable and "memory" in controllers,
        pids=usable and "pids" in controllers,
        io=False,
        level=EnforcementLevel.BEST_EFFORT if usable else EnforcementLevel.NONE,
        hard_limits=hard_limits,
        hard_ready=usable and {"memory", "pids"} <= controllers,
        slices_enabled=slices_enabled and usable,
        detail="" if available else prose,
    )


# -- slice names -------------------------------------------------------------


def principal_slice_token(principal_id: str) -> str:
    return hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:12]


def instance_slice_name() -> str:
    from kiro_crew import sandbox_cgroup

    return sandbox_cgroup._agents_slice_name()


def principal_slice_name(principal_id: str, *, instance_slice: str | None = None) -> str:
    parent = instance_slice or instance_slice_name()
    stem = parent[: -len(".slice")] if parent.endswith(".slice") else parent
    return f"{stem}-p{principal_slice_token(principal_id)}.slice"


# -- applying slice limits ---------------------------------------------------

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _run(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15
    )


def unit_active_state(unit: str, *, runner: Runner = _run) -> str | None:
    """``systemctl --user is-active <unit>``'s answer, or None when it cannot be asked.

    *unit* must be a unit name this module or ``sandbox_slice`` minted; anything
    with characters outside a unit name's alphabet is refused unasked.
    """
    if not _UNIT_NAME_RE.match(unit or ""):
        return None
    from kiro_crew import platform_compat

    systemctl = platform_compat.trusted_system_bin("systemctl")
    if systemctl is None:
        return None
    try:
        proc = runner([systemctl, "--user", "is-active", unit])
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (proc.stdout or "").strip()


_UNIT_NAME_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,200}\.(scope|slice)\Z")

#: Prefix of the runtime scope units ``sandbox_slice`` mints.
RUNTIME_SCOPE_PREFIX = "kirocrew-rt-"
_CGROUP_ROOT = "/sys/fs/cgroup"


def slice_populated(
    slice_name: str,
    *,
    runner: Runner = _run,
    cgroup_root: str = _CGROUP_ROOT,
    systemctl: str | None = None,
) -> bool | None:
    """Whether anything at all still runs under *slice_name*, or None when unknown.

    Reads the slice's own ``cgroup.events``, which counts every descendant
    (child slices and scopes alike). A slice that is not loaded holds nothing.
    Shells out and reads a file: call off-loop.
    """
    if not _UNIT_NAME_RE.match(slice_name or "") or not slice_name.endswith(".slice"):
        return None
    if systemctl is None:
        from kiro_crew import platform_compat

        systemctl = platform_compat.trusted_system_bin("systemctl")
    if systemctl is None:
        return None
    try:
        proc = runner([systemctl, "--user", "show", "-p", "ControlGroup", "--value", slice_name])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    group = (proc.stdout or "").strip()
    if not group:
        return False
    import os

    try:
        with open(
            os.path.join(cgroup_root, group.lstrip("/"), "cgroup.events"), encoding="utf-8"
        ) as fh:
            events = fh.read()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    for line in events.splitlines():
        key, _, value = line.partition(" ")
        if key == "populated":
            return value.strip() != "0"
    return None


def populated_scopes(
    slice_name: str, *, runner: Runner = _run, cgroup_root: str = _CGROUP_ROOT
) -> list[str] | None:
    """The runtime scopes under *slice_name* that still hold a process, or None.

    Reads the slice's control group from systemd and then each child scope's
    ``cgroup.events``: a scope counts while it is ``populated 1``. A slice that
    is not loaded holds nothing (``[]``). None means the question could not be
    asked (no trusted systemctl, an unreadable tree), which proves nothing.
    Shells out and reads files: call off-loop.
    """
    if not _UNIT_NAME_RE.match(slice_name or "") or not slice_name.endswith(".slice"):
        return None
    from kiro_crew import platform_compat

    systemctl = platform_compat.trusted_system_bin("systemctl")
    if systemctl is None:
        return None
    try:
        proc = runner([systemctl, "--user", "show", "-p", "ControlGroup", "--value", slice_name])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    group = (proc.stdout or "").strip()
    if not group:
        return []
    import os

    base = os.path.join(cgroup_root, group.lstrip("/"))
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return []
    except OSError:
        return None
    alive: list[str] = []
    for name in names:
        if not (name.startswith(RUNTIME_SCOPE_PREFIX) and name.endswith(".scope")):
            continue
        try:
            with open(os.path.join(base, name, "cgroup.events"), encoding="utf-8") as fh:
                events = fh.read()
        except FileNotFoundError:
            continue
        except OSError:
            return None
        if "populated 1" in events:
            alive.append(name)
    return alive


def stop_unit(unit: str, *, runner: Runner = _run) -> bool:
    """``systemctl --user stop <unit>`` for a unit this module or ``sandbox_slice`` minted."""
    if not _UNIT_NAME_RE.match(unit or ""):
        return False
    from kiro_crew import platform_compat

    systemctl = platform_compat.trusted_system_bin("systemctl")
    if systemctl is None:
        return False
    try:
        return runner([systemctl, "--user", "stop", unit]).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class SliceManager:
    """Creates and sizes per-principal slices. Every method shells out: call off-loop.

    A slice is applied once per (name, properties) for the process lifetime and
    remembered in :meth:`is_ready`, which ``sandbox_slice.cgroup_scope_argv``
    reads on the event loop without any I/O.
    """

    def __init__(self, *, runner: Runner = _run, systemctl: str | None = None) -> None:
        self._runner = runner
        self._systemctl = systemctl
        # _lock guards only _applied and is never held across a systemctl call,
        # because is_ready takes it on the event loop. _io_lock orders the
        # off-loop apply and release calls so a release cannot stop a slice an
        # apply is sizing.
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._applied: dict[str, tuple[str, ...]] = {}

    def _bin(self) -> str | None:
        if self._systemctl:
            return self._systemctl
        from kiro_crew import platform_compat

        return platform_compat.trusted_system_bin("systemctl")

    def populated(self, slice_name: str) -> bool | None:
        """:func:`slice_populated`, asked through this manager's runner and ``systemctl``."""
        return slice_populated(slice_name, runner=self._runner, systemctl=self._bin())

    def apply(self, slice_name: str, limits: SliceLimits, *, cpu_delegated: bool) -> bool:
        if not _UNIT_NAME_RE.match(slice_name or "") or not slice_name.endswith(".slice"):
            return False
        props = tuple(limits.properties(cpu_delegated=cpu_delegated))
        with self._lock:
            if self._applied.get(slice_name) == props:
                return True
        with self._io_lock:
            return self._apply_locked(slice_name, props)

    def _apply_locked(self, slice_name: str, props: tuple[str, ...]) -> bool:
        with self._lock:
            if self._applied.get(slice_name) == props:
                return True
        systemctl = self._bin()
        if systemctl is None:
            return False
        cmds = (
            [[systemctl, "--user", "set-property", "--runtime", slice_name, *props]]
            if props
            else []
        )
        cmds.append([systemctl, "--user", "start", slice_name])
        for cmd in cmds:
            try:
                proc = self._runner(cmd)
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("shared crew: could not size %s: %s", slice_name, exc)
                return False
            if proc.returncode != 0:
                logger.warning(
                    "shared crew: could not size %s (rc=%d): %s",
                    slice_name,
                    proc.returncode,
                    (proc.stderr or "").strip()[:300],
                )
                return False
        with self._lock:
            self._applied[slice_name] = props
        return True

    def reset_instance_quota(self) -> bool:
        """Clear only this feature's CPU quota, without stopping an occupied slice."""
        name = instance_slice_name()
        with self._io_lock:
            with self._lock:
                props = self._applied.get(name, ())
            remaining = tuple(prop for prop in props if not prop.startswith("CPUQuota="))
            if remaining == props:
                return True
            systemctl = self._bin()
            if systemctl is None:
                return False
            try:
                proc = self._runner(
                    [systemctl, "--user", "set-property", "--runtime", name, "CPUQuota="]
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            if proc.returncode != 0:
                return False
            with self._lock:
                self._applied[name] = remaining
            return True

    def is_ready(self, slice_name: str) -> bool:
        with self._lock:
            return slice_name in self._applied

    def forget(self, slice_name: str) -> None:
        with self._lock:
            self._applied.pop(slice_name, None)

    def applied(self) -> tuple[str, ...]:
        """The slices this process applied and has not released."""
        with self._lock:
            return tuple(self._applied)

    def release(
        self,
        slice_name: str,
        *,
        busy: Callable[[str], "list[str] | None"],
        owned_only: bool = True,
    ) -> bool:
        """Stop an idle *slice_name* and drop its runtime drop-ins; False when kept.

        Kept when *busy* names a populated scope or cannot tell (None): stopping a
        slice stops every scope in it. The slice is forgotten first, so a spawn
        racing the release lands in the instance slice (which keeps every
        ceiling) and the next admission sizes the slice again from scratch; a
        kept slice is remembered again. *owned_only* False also releases a slice
        an earlier process left behind (gateway start).
        """
        if not _UNIT_NAME_RE.match(slice_name or "") or not slice_name.endswith(".slice"):
            return False
        with self._io_lock:
            return self._release_locked(slice_name, busy=busy, owned_only=owned_only)

    def _release_locked(
        self, slice_name: str, *, busy: Callable[[str], "list[str] | None"], owned_only: bool
    ) -> bool:
        with self._lock:
            if owned_only and slice_name not in self._applied:
                return False
            props = self._applied.pop(slice_name, None)
        try:
            scopes = busy(slice_name)
        except Exception:  # noqa: BLE001 -- unknown is busy
            scopes = None
        systemctl = self._bin() if scopes == [] else None
        if systemctl is None:
            if props is not None:
                with self._lock:
                    self._applied[slice_name] = props
            return False
        ok = True
        for cmd in (
            [systemctl, "--user", "stop", slice_name],
            [systemctl, "--user", "revert", slice_name],
        ):
            try:
                ok = self._runner(cmd).returncode == 0 and ok
            except (OSError, subprocess.TimeoutExpired):
                ok = False
        return ok

    def principal_slices(self) -> list[str] | None:
        """Every loaded per-principal slice of this Crew's instance, or None."""
        systemctl = self._bin()
        if systemctl is None:
            return None
        stem = instance_slice_name()
        stem = stem[: -len(".slice")] if stem.endswith(".slice") else stem
        pattern = f"{stem}-p*.slice"
        try:
            proc = self._runner(
                [systemctl, "--user", "list-units", "--all", "--plain", "--no-legend", pattern]
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        names = []
        for line in (proc.stdout or "").splitlines():
            name = line.split()[0] if line.split() else ""
            if _UNIT_NAME_RE.match(name) and name.startswith(stem + "-p"):
                names.append(name)
        return names


async def reset_disabled_instance_quota(
    slices: SliceManager | None, limits: EffectiveLimits
) -> None:
    """Release the owned quota before disabling containment; a failure keeps admission on."""
    if slices is None or (limits.enabled and limits.containment_enabled):
        return
    if not await asyncio.to_thread(slices.reset_instance_quota):
        raise RuntimeError("shared crew: could not reset the instance CPU quota")


async def prepare_slices(
    slices: SliceManager | None, budget_key: str, lim: EffectiveLimits, report: ContainmentReport
) -> str:
    """Apply the Crew and principal ceilings; hard mode requires both to succeed."""
    if not lim.enabled or slices is None or not lim.containment_enabled or not report.available:
        return ""
    name = principal_slice_name(budget_key)
    limits = SliceLimits(
        cpu_quota_percent=lim.principal_cpu_quota_percent,
        memory_max_mb=lim.principal_memory_max_mb,
        tasks_max=lim.principal_tasks_max,
    )
    crew = SliceLimits(cpu_quota_percent=lim.crew_cpu_quota_percent)

    def apply() -> bool:
        if crew.cpu_quota_percent > 0 and report.cpu:
            crew_ok = slices.apply(instance_slice_name(), crew, cpu_delegated=True)
            if not crew_ok and lim.hard_limits:
                return False
        return slices.apply(name, limits, cpu_delegated=report.cpu)

    return name if await asyncio.to_thread(apply) else ""


# -- the hard-limit backstop ---------------------------------------------------


class ContainmentUnavailable(RuntimeError):
    """Hard-limit mode is on and a containment prerequisite is missing."""

    code = "containment_unavailable"

    def __init__(self, reason: str) -> None:
        super().__init__(f"containment unavailable: {reason}")
        self.reason = reason


#: The last report the admission service probed while hard-limit mode is on.
_HARD_MODE_REPORT: ContainmentReport | None = None


def publish_report(report: ContainmentReport | None) -> None:
    """Record the admission service's latest probe for :func:`hard_mode_refusal`."""
    global _HARD_MODE_REPORT
    _HARD_MODE_REPORT = report


def hard_mode_refusal() -> str | None:
    """The machine reason every runtime start is refused with, or None. In memory."""
    report = _HARD_MODE_REPORT
    return None if report is None else report.refusal_reason


# -- the principal a spawn belongs to -----------------------------------------

_SCOPE_PRINCIPAL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "shared_crew_scope_principal", default=None
)

#: Installed by the admission service while per-principal slices are active.
_ACTIVE_SLICES: SliceManager | None = None


def install_slice_manager(manager: SliceManager | None) -> None:
    global _ACTIVE_SLICES
    _ACTIVE_SLICES = manager


@contextlib.contextmanager
def principal_scope(principal_id: str | None) -> Iterator[None]:
    """Spawns made inside this block land in *principal_id*'s slice."""
    token = _SCOPE_PRINCIPAL.set(principal_id)
    try:
        yield
    finally:
        _SCOPE_PRINCIPAL.reset(token)


def create_task_in_scope(
    coro: Coroutine[Any, Any, T], principal_id: str | None, *, name: str | None = None
) -> "asyncio.Task[T]":
    """``asyncio.create_task`` whose task (and its spawns) run as *principal_id*."""
    ctx = contextvars.copy_context()
    ctx.run(_SCOPE_PRINCIPAL.set, principal_id)
    return asyncio.get_running_loop().create_task(coro, name=name, context=ctx)


def run_principal(info: Any, *, own_only: bool = False) -> str | None:
    """The principal a subagent run's runtime belongs to. In memory.

    An attached run's own key (``subagent:<run id>``, unique to this run) is
    bound to its admitted budget, which never changes; a delayed start (an
    approval, a queue release) reads that, not the parent session's latest
    sender. Before the attach, the parent session decides, unless *own_only*.
    """
    from kiro_crew.shared_crew.admission_gate import scope_principal_for_session

    run_id = str(getattr(info, "id", "") or "")
    principal = scope_principal_for_session(f"subagent:{run_id}") if run_id else None
    if principal is not None or own_only:
        return principal
    return scope_principal_for_session(str(getattr(info, "parent_session_key", "") or ""))


def start_run_task(coro: Coroutine[Any, Any, T], info: Any) -> "asyncio.Task[T]":
    """Start a subagent run's task inside its initiator's containment scope.

    With Shared Crew admission on, the run's runtime spawn lands in the slice of
    the person the run was admitted for (its own binding once attached, else
    the person whose work is running in the parent session); otherwise this is
    ``asyncio.create_task``.
    """
    principal = run_principal(info)
    if principal is None:
        return asyncio.create_task(coro)
    return create_task_in_scope(coro, principal)


def scope_parent_slice() -> str | None:
    """The slice a new runtime scope should join, or None for the instance slice.

    Read by ``sandbox_slice.cgroup_scope_argv`` on the event loop: an in-memory
    check only. A principal whose slice has not been applied yet gets the
    instance slice, which still carries every existing ceiling.
    """
    manager = _ACTIVE_SLICES
    principal = _SCOPE_PRINCIPAL.get()
    if manager is None or not principal:
        return None
    try:
        name = principal_slice_name(principal)
    except Exception:
        return None
    return name if manager.is_ready(name) else None


__all__ = [
    "ContainmentReport",
    "ContainmentUnavailable",
    "REASON_ADMISSION_OFF",
    "admission_off_report",
    "hard_mode_refusal",
    "populated_scopes",
    "publish_report",
    "RUNTIME_SCOPE_PREFIX",
    "stop_unit",
    "SliceLimits",
    "SliceManager",
    "classify_probe_reason",
    "create_task_in_scope",
    "install_slice_manager",
    "instance_slice_name",
    "principal_scope",
    "principal_slice_name",
    "principal_slice_token",
    "probe_containment",
    "run_principal",
    "scope_parent_slice",
    "start_run_task",
    "unit_active_state",
]
