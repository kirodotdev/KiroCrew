"""Unit tests for the cloud size-tier catalog (cloud/sizes.py)."""

from __future__ import annotations

import pytest

from kiro_crew.cloud import sizes


class TestTierCatalog:
    def test_default_is_recommended_and_32gb(self):
        d = sizes.default_tier()
        assert d.key == sizes.DEFAULT_TIER_KEY
        assert d.recommended is True
        # Default is the 8-vCPU "Development" tier: 16 GB caps sub-agents at the
        # floor (CPU-bound), so the default steps up to 32 GB / 8 vCPU.
        assert d.ram_gb == 32
        assert d.vcpu == 8

    def test_default_is_arm64(self):
        assert sizes.default_tier().arch == sizes.ARCH_ARM64

    def test_no_tier_below_working_set(self):
        # 8 GB is below Kiro Crew's ~10 GB working set and is not offered.
        for t in sizes.all_tiers():
            assert t.ram_gb >= 16
            assert t.disk_gb >= 40
            assert t.vcpu >= 4
            assert t.approx_usd_per_hr > 0

    def test_ladder_is_cpu_scaled(self):
        # Each arm tier doubles both RAM and vCPU so the sub-agent cap actually
        # rises (the cap is CPU-bound). Power must be 16 vCPU, not a memory shape.
        light, dev, power = (sizes.get_tier(k) for k in ("light", "balanced", "power"))
        assert (light.ram_gb, light.vcpu) == (16, 4)
        assert (dev.ram_gb, dev.vcpu) == (32, 8)
        assert (power.ram_gb, power.vcpu) == (64, 16)
        assert power.instance_type == "m7g.4xlarge"

    def test_get_tier_known(self):
        assert sizes.get_tier("light").instance_type == "t4g.xlarge"
        assert sizes.get_tier("power").instance_type == "m7g.4xlarge"

    def test_get_tier_unknown_lists_valid(self):
        with pytest.raises(KeyError) as ei:
            sizes.get_tier("nope")
        assert "unknown size 'nope'" in str(ei.value)
        assert "balanced" in str(ei.value)

    def test_interactive_tiers_order(self):
        keys = [t.key for t in sizes.interactive_tiers()]
        assert keys == ["light", "balanced", "power"]

    def test_x86_lane_present(self):
        for k in ("light-x86", "balanced-x86", "power-x86"):
            assert sizes.get_tier(k).arch == sizes.ARCH_X86_64

    def test_summary_mentions_specs(self):
        s = sizes.get_tier("balanced").summary()
        assert "m7g.2xlarge" in s
        assert "32 GB" in s
        assert "arm64" in s

    def test_monthly_estimate(self):
        t = sizes.get_tier("balanced")
        # 24h/day * 30 days
        assert sizes.monthly_estimate(t) == round(t.approx_usd_per_hr * 24 * 30, 2)
        # Half-day uptime is roughly half.
        assert sizes.monthly_estimate(t, 12) < sizes.monthly_estimate(t, 24)
