"""Custom EC2 instance types for the built-in Remote Crew launch.

The size tiers in :mod:`kiro_crew.cloud.sizes` stay the default path. This
module backs the launch form's advanced "custom instance type" choice: it lists
the instance types a Region **offers** that fit a Crew host, and turns one the
user picked into a :class:`sizes.SizeTier` the launcher can deploy.

"Offered" comes from ``describe-instance-type-offerings``: AWS sells the type in
that zone. It says nothing about free capacity right now, which is known only
when the launch runs (``ec2.deploy`` then moves to another zone).

Every read goes through :func:`aws.checked_json`, so the same profile, Region,
sandbox and error mapping apply as for the launch itself. Listings are cached
briefly per (profile, Region): two read calls that page through every type are
slow, and the form asks again on every open.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from kiro_crew.cloud import aws, sizes

#: Families a Crew host may run on: general purpose (m, t), compute (c) and
#: memory (r). A family is one of these letters followed by its generation digit,
#: so ``trn1`` or ``r`` alone never match.
CREW_FAMILY_LETTERS = frozenset("mtcr")
#: Kiro Crew needs at least 16 GB of memory: its working set is about 10 GB with
#: spikes past it, the floor every size tier in ``sizes`` already keeps.
MIN_MEMORY_GIB = 16
#: Root volume for a custom type; the same as the default tier's.
CUSTOM_DISK_GB = sizes.TIERS_BY_KEY[sizes.DEFAULT_TIER_KEY].disk_gb
#: Key a custom tier carries; never a ``--size`` value.
CUSTOM_TIER_KEY = "custom"
#: How long one listing is reused for the same (profile, Region).
CACHE_TTL_SECONDS = 300.0
#: An EC2 instance type name is short: family, dot, size (``m7i-flex.12xlarge``).
MAX_NAME_LEN = 32

_ARCHES = (sizes.ARCH_ARM64, sizes.ARCH_X86_64)
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


@dataclass(frozen=True)
class OfferedType:
    """One instance type the Region offers that fits a Crew host."""

    instance_type: str
    vcpu: int
    memory_gib: float
    arch: str
    zones: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "instance_type": self.instance_type,
            "vcpu": self.vcpu,
            "memory_gib": self.memory_gib,
            "arch": self.arch,
            "zones": list(self.zones),
        }


def validate_name(name: str) -> str:
    """Return ``name`` when it is shaped like an EC2 instance type, else raise.

    The value reaches the ``aws`` argv and the stack parameters, so it is checked
    by its characters: lowercase letters, digits and ``-`` around exactly one dot.
    """
    name = name.strip()
    family, dot, size = name.partition(".")
    ok = (
        0 < len(name) <= MAX_NAME_LEN
        and dot == "."
        and bool(family)
        and bool(size)
        and family[0].isalpha()
        and all(c in _NAME_CHARS for c in family)
        and all(c in _NAME_CHARS for c in size)
    )
    if not ok:
        raise ValueError(f"{name!r} is not an EC2 instance type name (for example m7i.xlarge)")
    return name


def is_crew_family(name: str) -> bool:
    """Whether ``name`` belongs to a family a Crew host may run on."""
    family = name.partition(".")[0]
    return len(family) >= 2 and family[0] in CREW_FAMILY_LETTERS and family[1].isdigit()


def _zones_by_type(profile: str, region: str) -> dict[str, set[str]]:
    data = aws.checked_json(
        ["ec2", "describe-instance-type-offerings", "--location-type", "availability-zone"],
        profile,
        region,
        action="ec2:DescribeInstanceTypeOfferings",
    )
    zones: dict[str, set[str]] = {}
    offerings = data.get("InstanceTypeOfferings", []) if isinstance(data, dict) else []
    for o in offerings:
        name, zone = str(o.get("InstanceType", "")), str(o.get("Location", ""))
        if name and zone and is_crew_family(name):
            zones.setdefault(name, set()).add(zone)
    return zones


def _type_facts(profile: str, region: str) -> list[dict]:
    data = aws.checked_json(
        [
            "ec2",
            "describe-instance-types",
            "--filters",
            "Name=current-generation,Values=true",
            "Name=bare-metal,Values=false",
        ],
        profile,
        region,
        action="ec2:DescribeInstanceTypes",
    )
    return list(data.get("InstanceTypes", [])) if isinstance(data, dict) else []


def _arch_of(facts: dict) -> str:
    supported = (facts.get("ProcessorInfo") or {}).get("SupportedArchitectures") or []
    return next((a for a in _ARCHES if a in supported), "")


def _read_offered(profile: str, region: str) -> list[OfferedType]:
    zones = _zones_by_type(profile, region)
    rows = []
    for facts in _type_facts(profile, region):
        name = str(facts.get("InstanceType", ""))
        if name not in zones:
            continue
        arch = _arch_of(facts)
        vcpu = int((facts.get("VCpuInfo") or {}).get("DefaultVCpus") or 0)
        mib = int((facts.get("MemoryInfo") or {}).get("SizeInMiB") or 0)
        if not arch or vcpu < 1 or mib < 1:
            continue
        rows.append(OfferedType(name, vcpu, round(mib / 1024, 1), arch, tuple(sorted(zones[name]))))
    rows.sort(key=lambda r: (r.instance_type.partition(".")[0], r.vcpu, r.memory_gib))
    return rows


_cache: dict[tuple[str, str], tuple[float, list[OfferedType]]] = {}
_cache_lock = threading.Lock()


def list_offered(
    profile: str, region: str, *, clock: Callable[[], float] = time.monotonic
) -> list[OfferedType]:
    """The instance types ``region`` offers that fit a Crew host, cached briefly.

    Only types with at least ``MIN_MEMORY_GIB`` are listed. Raises
    :class:`aws.AWSError` when AWS cannot be read; nothing is cached then.
    """
    return [
        r for r in _offered_any_memory(profile, region, clock) if r.memory_gib >= MIN_MEMORY_GIB
    ]


def _offered_any_memory(profile: str, region: str, clock: Callable[[], float]) -> list[OfferedType]:
    """Every offered Crew-family type, whatever its memory; the cached read."""
    key = (profile, region)
    now = clock()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    rows = _read_offered(profile, region)
    with _cache_lock:
        _cache[key] = (now, rows)
    return rows


def clear_cache() -> None:
    """Forget every cached listing."""
    with _cache_lock:
        _cache.clear()


def find_offered(name: str, profile: str, region: str) -> OfferedType:
    """The listing row for ``name``; raises ``ValueError`` with a plain reason.

    Raises :class:`aws.AWSError` when the Region's offerings cannot be read.
    """
    name = validate_name(name)
    if not is_crew_family(name):
        raise ValueError(
            f"{name} is not in a family Kiro Crew runs on (general purpose m or t, "
            "compute c, or memory r)"
        )
    rows = _offered_any_memory(profile, region, time.monotonic)
    row = next((r for r in rows if r.instance_type == name), None)
    if row is None:
        raise ValueError(
            f"{name} is not offered in {region}: pick a type from the list for this Region"
        )
    if row.memory_gib < MIN_MEMORY_GIB:
        raise ValueError(
            f"Kiro Crew needs at least {MIN_MEMORY_GIB} GB memory, and {name} has "
            f"{row.memory_gib:g} GiB: pick a larger type"
        )
    return row


def custom_tier(name: str, profile: str, region: str) -> sizes.SizeTier:
    """A size tier for the offered type ``name``; its architecture picks the AMI."""
    row = find_offered(name, profile, region)
    return sizes.SizeTier(
        key=CUSTOM_TIER_KEY,
        label=row.instance_type,
        instance_type=row.instance_type,
        arch=row.arch,
        vcpu=row.vcpu,
        ram_gb=int(row.memory_gib),
        disk_gb=CUSTOM_DISK_GB,
        approx_usd_per_hr=0.0,
    )
