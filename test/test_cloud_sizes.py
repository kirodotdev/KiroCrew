"""Unit tests for the cloud size-tier catalog (cloud/sizes.py)."""

from __future__ import annotations

import pytest

from kiro_crew.cloud import sizes


class TestTierCatalog:
    def test_default_is_recommended_and_16gb(self):
        d = sizes.default_tier()
        assert d.key == sizes.DEFAULT_TIER_KEY
        assert d.recommended is True
        # KiroCrew uses ~10 GB; default must have headroom.
        assert d.ram_gb >= 16

    def test_default_is_arm64(self):
        assert sizes.default_tier().arch == sizes.ARCH_ARM64

    def test_all_arm_tiers_meet_or_exceed_minimum(self):
        for t in sizes.all_tiers():
            assert t.ram_gb >= 8
            assert t.disk_gb >= 30
            assert t.vcpu >= 2
            assert t.approx_usd_per_hr > 0

    def test_get_tier_known(self):
        assert sizes.get_tier("light").instance_type == "t4g.large"
        assert sizes.get_tier("power").instance_type == "m7g.2xlarge"

    def test_get_tier_unknown_lists_valid(self):
        with pytest.raises(KeyError) as ei:
            sizes.get_tier("nope")
        assert "unknown size 'nope'" in str(ei.value)
        assert "balanced" in str(ei.value)

    def test_interactive_tiers_order(self):
        keys = [t.key for t in sizes.interactive_tiers()]
        assert keys == ["light", "balanced", "power"]

    def test_x86_lane_present(self):
        assert sizes.get_tier("balanced-x86").arch == sizes.ARCH_X86_64
        assert sizes.get_tier("power-x86").arch == sizes.ARCH_X86_64

    def test_summary_mentions_specs(self):
        s = sizes.get_tier("balanced").summary()
        assert "t4g.xlarge" in s
        assert "16 GB" in s
        assert "arm64" in s

    def test_monthly_estimate(self):
        t = sizes.get_tier("balanced")
        # 24h/day * 30 days
        assert sizes.monthly_estimate(t) == round(t.approx_usd_per_hr * 24 * 30, 2)
        # Half-day uptime is roughly half.
        assert sizes.monthly_estimate(t, 12) < sizes.monthly_estimate(t, 24)
