"""Instance size tiers for the cloud launcher (constants — no magic numbers).

KiroCrew itself uses ~10 GB RAM with spikes beyond that (see
``docs/REMOTE_DESKTOP_SETUP.md``), so the default tier is **≥16 GB RAM**. We
default to **arm64 / Graviton** (cheaper per GB; both KiroCrew and ``kiro-cli``
ship aarch64 Linux builds), with an x86_64 lane for users who need it.

Prices are illustrative on-demand USD/hour for the tier's default region and are
always surfaced to the user as "approximate" — never used for anything but
display.
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


# arm64 / Graviton tiers (the default lane). t4g is burstable — cheap at idle,
# bursts for tool calls; the Power tier uses non-burstable m7g for sustained load.
_TIERS: tuple[SizeTier, ...] = (
    SizeTier(
        key="light",
        label="Light",
        instance_type="t4g.large",
        arch=ARCH_ARM64,
        vcpu=2,
        ram_gb=8,
        disk_gb=30,
        approx_usd_per_hr=0.067,
    ),
    SizeTier(
        key="balanced",
        label="Balanced",
        instance_type="t4g.xlarge",
        arch=ARCH_ARM64,
        vcpu=4,
        ram_gb=16,
        disk_gb=40,
        approx_usd_per_hr=0.134,
        recommended=True,
    ),
    SizeTier(
        key="power",
        label="Power",
        instance_type="m7g.2xlarge",
        arch=ARCH_ARM64,
        vcpu=8,
        ram_gb=32,
        disk_gb=60,
        approx_usd_per_hr=0.326,
    ),
    # x86_64 lane — same shapes, for users who need Intel/AMD.
    SizeTier(
        key="balanced-x86",
        label="Balanced (x86_64)",
        instance_type="t3.xlarge",
        arch=ARCH_X86_64,
        vcpu=4,
        ram_gb=16,
        disk_gb=40,
        approx_usd_per_hr=0.166,
    ),
    SizeTier(
        key="power-x86",
        label="Power (x86_64)",
        instance_type="m7i.2xlarge",
        arch=ARCH_X86_64,
        vcpu=8,
        ram_gb=32,
        disk_gb=60,
        approx_usd_per_hr=0.403,
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
