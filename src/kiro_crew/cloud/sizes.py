"""Instance size tiers for the cloud launcher (constants — no magic numbers).

Kiro Crew uses ~10 GB RAM with spikes beyond that (see
``docs/guides/remote-and-mobile.md``), so every tier is **≥16 GB RAM** — 8 GB is
below the working set and gets OOM-killed under load, so it is not offered.

The tiers are laddered by **how much runs at once**, not by GB: the sub-agent
concurrency cap is CPU-bound (``subagent.py`` derives it from vCPU), so RAM alone
does not raise it. Effective parallel sub-agents per tier:

* **Light** — 16 GB / 4 vCPU  → ~3 sub-agents (the floor); single-threaded work.
* **Development** (default, recommended) — 32 GB / 8 vCPU → ~6 sub-agents; the
  normal working setup.
* **Power** — 64 GB / **16 vCPU** → ~12 sub-agents; large fan-outs, long builds.
  (The vCPU count is what matters — a 64 GB box with only 8 vCPU would still cap
  at ~6, so the Power tier is ``m7g.4xlarge`` / 16 vCPU, not a memory-optimized
  shape.)

We default to **arm64 / Graviton** (cheaper per GB; both Kiro Crew and ``kiro-cli``
ship aarch64 Linux builds), with an x86_64 lane for users who need it.

Prices are illustrative on-demand USD/hour and are surfaced only as "approximate"
in the CLI — never used for anything but display, and the dashboard shows no
dollar figure at all (it links the AWS Pricing Calculator instead).
"""

from __future__ import annotations

from dataclasses import dataclass

# CloudFormation architecture parameter values (also select the AMI alias).
ARCH_ARM64 = "arm64"
ARCH_X86_64 = "x86_64"


@dataclass(frozen=True)
class SizeTier:
    """One selectable instance size."""

    key: str  # stable id used on the CLI (--size) and in config
    label: str  # short human label
    instance_type: str  # EC2 instance type
    arch: str  # ARCH_ARM64 | ARCH_X86_64
    vcpu: int
    ram_gb: int
    disk_gb: int  # gp3 root volume size
    approx_usd_per_hr: float  # illustrative on-demand price
    recommended: bool = False

    def summary(self) -> str:
        """One-line human summary for menus and confirmations."""
        arch = "arm64" if self.arch == ARCH_ARM64 else "x86_64"
        return (
            f"{self.instance_type}  {arch} · {self.ram_gb} GB · {self.vcpu} vCPU · "
            f"{self.disk_gb} GB disk  ~${self.approx_usd_per_hr:.2f}/hr"
        )


# arm64 / Graviton tiers (the default lane). Light is burstable t4g (cheap at
# idle, bursts for tool calls); Development/Power use non-burstable m7g for
# sustained parallel load. Keys (light/balanced/power) are stable CLI --size ids
# — the ladder was raised (8 GB retired) but the keys did not change, so existing
# --size values and saved configs keep resolving.
_TIERS: tuple[SizeTier, ...] = (
    SizeTier(
        key="light",
        label="Light",
        instance_type="t4g.xlarge",
        arch=ARCH_ARM64,
        vcpu=4,
        ram_gb=16,
        disk_gb=40,
        approx_usd_per_hr=0.134,
    ),
    SizeTier(
        key="balanced",
        label="Development",
        instance_type="m7g.2xlarge",
        arch=ARCH_ARM64,
        vcpu=8,
        ram_gb=32,
        disk_gb=60,
        approx_usd_per_hr=0.326,
        recommended=True,
    ),
    SizeTier(
        key="power",
        label="Power",
        instance_type="m7g.4xlarge",
        arch=ARCH_ARM64,
        vcpu=16,
        ram_gb=64,
        disk_gb=80,
        approx_usd_per_hr=0.653,
    ),
    # x86_64 lane — same shapes, for users who need Intel/AMD.
    SizeTier(
        key="light-x86",
        label="Light (x86_64)",
        instance_type="t3.xlarge",
        arch=ARCH_X86_64,
        vcpu=4,
        ram_gb=16,
        disk_gb=40,
        approx_usd_per_hr=0.166,
    ),
    SizeTier(
        key="balanced-x86",
        label="Development (x86_64)",
        instance_type="m7i.2xlarge",
        arch=ARCH_X86_64,
        vcpu=8,
        ram_gb=32,
        disk_gb=60,
        approx_usd_per_hr=0.403,
    ),
    SizeTier(
        key="power-x86",
        label="Power (x86_64)",
        instance_type="m7i.4xlarge",
        arch=ARCH_X86_64,
        vcpu=16,
        ram_gb=64,
        disk_gb=80,
        approx_usd_per_hr=0.806,
    ),
)

TIERS_BY_KEY: dict[str, SizeTier] = {t.key: t for t in _TIERS}

DEFAULT_TIER_KEY = "balanced"

# The tiers offered in the interactive picker, in display order (the x86 lane is
# reachable via --size but kept out of the default 3-choice menu for simplicity).
INTERACTIVE_TIER_KEYS = ("light", "balanced", "power")


def all_tiers() -> tuple[SizeTier, ...]:
    """All defined tiers (arm64 lane first, then x86)."""
    return _TIERS


def interactive_tiers() -> list[SizeTier]:
    """The tiers shown in the wizard's size picker."""
    return [TIERS_BY_KEY[k] for k in INTERACTIVE_TIER_KEYS]


def get_tier(key: str) -> SizeTier:
    """Resolve a tier by key; raise ``KeyError`` with the valid set if unknown."""
    try:
        return TIERS_BY_KEY[key]
    except KeyError:
        valid = ", ".join(TIERS_BY_KEY)
        raise KeyError(f"unknown size '{key}' (choose one of: {valid})") from None


def default_tier() -> SizeTier:
    """The recommended default tier."""
    return TIERS_BY_KEY[DEFAULT_TIER_KEY]


def monthly_estimate(tier: SizeTier, hours_per_day: float = 24.0) -> float:
    """Approximate USD/month for a tier at the given daily uptime (display only)."""
    return round(tier.approx_usd_per_hr * hours_per_day * 30, 2)
