"""The dashboard's Shared Crew admission sub-section: ``SharedCrewConfig`` and its parser.

``dashboard.shared_crew`` holds the Shared Crew switches and limits: fair
admission, per-principal containment slices, the direct tailnet link and the
typing lock. Fair admission and the direct tailnet link are off by default,
and the slices apply only while fair admission is on. Every value is
bounded at parse time, and ``effective_limits`` is the one place an ``auto``
value is resolved, so the capacity API reports exactly what the gate enforces.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.config.fields import _meta, _safe_bool, _safe_dict, _safe_int
from kiro_crew.config.sections_defaults import SectionReader

logger = logging.getLogger("kiro_crew.config.loader")

#: Bounds for every integer knob, ``(lo, hi)``. A value outside is clamped.
SHARED_CREW_BOUNDS: dict[str, tuple[int, int]] = {
    "principal_concurrency": (1, 256),
    "crew_concurrency": (0, 1024),
    "typing_idle_seconds": (3, 60),
}


@dataclass
class SharedCrewConfig:
    """Fair admission and per-principal containment for a Crew several people use."""

    admission_enabled: bool = field(
        default=False,
        metadata=_meta(
            "Fair Admission",
            "Charge every turn, subagent, task, workflow and cron run to the person "
            "who started it, and admit work through per-person and Crew-wide limits "
            "with a fair queue. Off by default: a single-operator Crew keeps its "
            "current behaviour and reports queueing as unavailable.",
        ),
    )
    principal_concurrency: int = field(
        default=4,
        metadata=_meta(
            "Active Work Per Person",
            "Root executions (turns, tasks, workflows, cron runs) one person may run "
            "at once, across all tabs, devices and sessions.",
        ),
    )
    crew_concurrency: int = field(
        default=0,
        metadata=_meta(
            "Active Work Crew-Wide",
            "Root executions the whole Crew runs at once. 0 uses the session start "
            "gate's automatic width for this host.",
        ),
    )
    containment_enabled: bool = field(
        default=True,
        metadata=_meta(
            "Per-Person Slices",
            "While fair admission is on, start each person's agent runtimes in that "
            "person's own systemd slice so their CPU, memory and process counts are "
            "bounded together. Linux with cgroup v2 delegation only.",
        ),
    )
    hard_limits: bool = field(
        default=False,
        metadata=_meta(
            "Require Enforced Limits",
            "Refuse to admit work when cgroup v2, a trusted systemd-run, a reachable "
            "user bus, or memory and pids delegation is missing, instead of running "
            "without per-person limits. Needs fair admission on: with fair admission "
            "off, no agent runtime starts. Off by default.",
        ),
    )
    tailnet_direct: bool = field(
        default=False,
        metadata=_meta(
            "Direct Tailnet Link",
            "Not active in this build yet. Off by default. On: while Tailscale runs, also listen on this "
            "machine's own Tailscale addresses (100.x and fd7a:) on the dashboard "
            "port, and let in the people Tailscale names (all of them, or only "
            "those Crew admits when local_authorization is on), without a "
            "dashboard token. Never binds any other address. Off keeps the "
            "loopback-only listener. A value that is not true or false reads as "
            "off.",
            restart=True,
        ),
    )
    local_authorization: bool = field(
        default=False,
        metadata=_meta(
            "Approve People In Crew",
            "Not active in this build yet. "
            "Off: Tailscale decides who gets in. Every person Tailscale lets reach "
            "this machine is admitted on the direct tailnet link and through "
            "tailscale serve, and is still named by Tailscale. On: only you, "
            "people who accepted a Crew invite and people you approved on People "
            "get in, and People shows Approve, Remove and the waiting list. "
            "Tagged nodes are refused unless carried over in both modes. A value "
            "that is not true or false reads as on.",
            restart=True,
        ),
    )
    typing_idle_seconds: int = field(
        default=10,
        metadata=_meta(
            "Typing Lock Idle Window",
            "Seconds a collaborator may pause typing before the other collaborators' "
            "composers unlock. While someone types in a shared session, everyone "
            "else's composer is read-only until that person sends, clears the "
            "draft, or stays idle this long. 3 to 60.",
        ),
    )


#: Fixed admission and slice limits. Tests change one with ``monkeypatch.setattr``.
PRINCIPAL_CHILD_LIMIT = 8
PRINCIPAL_QUEUE_DEPTH = 16
CREW_QUEUE_DEPTH = 64
QUEUE_EXPIRY_SECONDS = 300
ROOT_DEADLINE_SECONDS = 1800
CHILD_DEADLINE_SECONDS = 600
MAX_REQUEST_BYTES = 4 * 1024 * 1024
SESSION_CREATES_PER_MINUTE = 30
PRINCIPAL_STORAGE_MB = 2048
PRINCIPAL_CPU_QUOTA_PERCENT = 800
PRINCIPAL_MEMORY_MAX_MB = 24576
PRINCIPAL_TASKS_MAX = 8192
CREW_CPU_QUOTA_PERCENT = 2400

#: The boolean knobs read with ``_safe_bool``. ``tailnet_direct`` and
#: ``local_authorization`` are booleans read fail-narrow on their own; every other
#: field is an integer bounded by SHARED_CREW_BOUNDS.
SHARED_CREW_FLAGS = ("admission_enabled", "containment_enabled", "hard_limits")
#: Flags that widen what can reach this machine. A value that is not a boolean
#: reads as False, so a typo can only narrow exposure.
SHARED_CREW_EXPOSURE_FLAGS = ("tailnet_direct",)


def shared_crew_config_from(raw: object) -> SharedCrewConfig:
    """Build a bounded :class:`SharedCrewConfig` from a raw ``dashboard.shared_crew`` value.

    A non-object reads as every default. A wrong-typed key reads as its default,
    except the two exposure switches, which read toward less exposure:
    ``tailnet_direct`` as off and ``local_authorization`` as on. An out-of-range
    integer is clamped into :data:`SHARED_CREW_BOUNDS`, so a hand edit can narrow
    or widen a limit but never disable one.
    """
    data = _safe_dict(raw)
    section = SectionReader(SharedCrewConfig, data)
    values: dict[str, Any] = {}
    for name in SHARED_CREW_FLAGS:
        values[name] = section.read(name, _safe_bool)
    for name in SHARED_CREW_EXPOSURE_FLAGS:
        if name not in data:
            values[name] = section.default(name)
            continue
        raw = data.get(name)
        if not isinstance(raw, bool):
            logger.warning(
                "config: 'dashboard.shared_crew.%s' is not true or false (got %s); "
                "reading it as false",
                name,
                type(raw).__name__,
            )
        values[name] = raw is True
    # Turning it off widens who gets in, so only a literal false does that.
    values["local_authorization"] = data.get("local_authorization", False) is not False
    for name in SHARED_CREW_BOUNDS:
        lo, hi = SHARED_CREW_BOUNDS[name]
        values[name] = section.read(name, _safe_int, lo, hi)
    return SharedCrewConfig(**values)


@dataclass(frozen=True)
class EffectiveLimits:
    """The limits the admission gate enforces, with every ``auto`` resolved."""

    enabled: bool
    principal_active: int
    principal_children: int
    principal_queue: int
    crew_active: int
    crew_children: int
    crew_queue: int
    queue_expiry_seconds: int
    root_deadline_seconds: int
    child_deadline_seconds: int
    max_request_bytes: int
    session_creates_per_minute: int
    principal_storage_bytes: int
    containment_enabled: bool
    hard_limits: bool
    principal_cpu_quota_percent: int
    principal_memory_max_mb: int
    principal_tasks_max: int
    crew_cpu_quota_percent: int

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def effective_limits(cfg: SharedCrewConfig, *, auto_crew_concurrency: int) -> EffectiveLimits:
    """Resolve *cfg* against the host's automatic Crew width.

    ``crew_concurrency == 0`` takes *auto_crew_concurrency* (the session start
    gate's own automatic value), subagents get twice the resolved Crew width,
    and a per-person limit is never wider than the Crew's. The limits that are
    not knobs come from this module's constants.
    """
    crew_active = cfg.crew_concurrency or max(1, int(auto_crew_concurrency))
    crew_children = 2 * crew_active
    return EffectiveLimits(
        enabled=cfg.admission_enabled,
        principal_active=min(cfg.principal_concurrency, crew_active),
        principal_children=min(PRINCIPAL_CHILD_LIMIT, crew_children),
        principal_queue=min(PRINCIPAL_QUEUE_DEPTH, CREW_QUEUE_DEPTH),
        crew_active=crew_active,
        crew_children=crew_children,
        crew_queue=CREW_QUEUE_DEPTH,
        queue_expiry_seconds=QUEUE_EXPIRY_SECONDS,
        root_deadline_seconds=ROOT_DEADLINE_SECONDS,
        child_deadline_seconds=CHILD_DEADLINE_SECONDS,
        max_request_bytes=MAX_REQUEST_BYTES,
        session_creates_per_minute=SESSION_CREATES_PER_MINUTE,
        principal_storage_bytes=PRINCIPAL_STORAGE_MB * 1024 * 1024,
        containment_enabled=cfg.containment_enabled,
        hard_limits=cfg.hard_limits,
        principal_cpu_quota_percent=PRINCIPAL_CPU_QUOTA_PERCENT,
        principal_memory_max_mb=PRINCIPAL_MEMORY_MAX_MB,
        principal_tasks_max=PRINCIPAL_TASKS_MAX,
        crew_cpu_quota_percent=CREW_CPU_QUOTA_PERCENT,
    )


def load_shared_crew_config() -> SharedCrewConfig:
    """Read the effective ``dashboard.shared_crew`` section. Synchronous file I/O."""
    import json

    from kiro_crew.config.loader import config_local_path, config_path, read_config_text
    from kiro_crew.config.resolution import _deep_merge

    raw: dict[str, Any] = {}
    for path in (config_path(), config_local_path()):
        try:
            document = json.loads(read_config_text(path))
        except FileNotFoundError:
            continue
        if not isinstance(document, dict):
            raise ValueError("config must be an object")
        raw = _deep_merge(raw, document)
    dashboard = raw.get("dashboard", {})
    if not isinstance(dashboard, dict):
        raise ValueError("dashboard must be an object")
    section = dashboard.get("shared_crew", {})
    if not isinstance(section, dict):
        raise ValueError("dashboard.shared_crew must be an object")
    return shared_crew_config_from(section)


__all__ = [
    "EffectiveLimits",
    "SHARED_CREW_BOUNDS",
    "SHARED_CREW_EXPOSURE_FLAGS",
    "SHARED_CREW_FLAGS",
    "SharedCrewConfig",
    "effective_limits",
    "load_shared_crew_config",
    "shared_crew_config_from",
]
