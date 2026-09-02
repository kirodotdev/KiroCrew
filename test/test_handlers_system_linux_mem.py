"""Tests for the Linux /proc/meminfo path in handlers_system.py.

Regression guard for the "used memory over-reported" bug: the old estimate
``MemFree + Buffers + Cached`` omitted reclaimable slab (``SReclaimable``) and
counted it as used, inflating ``mem_used_gb`` by the whole slab cache (tens of
GB on hosts with large dentry/inode caches). The fix prefers the kernel's own
``MemAvailable`` (which already discounts reclaimable slab + page cache) and
adds ``SReclaimable`` to the legacy fallback.
"""

from __future__ import annotations

import builtins
from io import StringIO
from unittest.mock import patch

from kiro_crew.dashboard import handlers_system

_KB = 1024  # /proc/meminfo values are in kB; 1 GiB = 1024**2 kB
_GIB_KB = 1024 * 1024


def _fake_open_for_meminfo(meminfo_text: str):
    """Return an ``open`` replacement that serves fake /proc/meminfo and
    delegates every other path (e.g. /proc/stat) to the real open."""
    real_open = builtins.open

    def _fake_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path == "/proc/meminfo":
            return StringIO(meminfo_text)
        return real_open(path, *args, **kwargs)

    return _fake_open


def _collect_with_meminfo(meminfo_text: str) -> dict:
    """Run _collect_system_metrics hermetically with an injected /proc/meminfo."""
    handlers_system._metrics_cache = {}
    handlers_system._metrics_cache_ts = 0.0
    with (
        patch("sys.platform", "linux"),
        patch.object(handlers_system, "_get_static_system_info", return_value={}),
        patch.object(handlers_system, "_system_cpu_pct_from_proc_stat", return_value=5.0),
        patch("builtins.open", side_effect=_fake_open_for_meminfo(meminfo_text)),
    ):
        return handlers_system._collect_system_metrics()


class TestLinuxMemAvailable:
    def test_prefers_mem_available_over_free_plus_cache(self) -> None:
        """used = total - MemAvailable, NOT total - (MemFree+Buffers+Cached).

        Here MemFree+Buffers+Cached = 50 GiB (old "free"), but MemAvailable is
        65 GiB because 15 GiB of the 40 GiB cache/slab is reclaimable. The fix
        must report 35 GiB used, not the inflated 50 GiB.
        """
        meminfo = (
            f"MemTotal:       {100 * _GIB_KB} kB\n"
            f"MemFree:        {10 * _GIB_KB} kB\n"
            f"Buffers:        0 kB\n"
            f"Cached:         {40 * _GIB_KB} kB\n"
            f"MemAvailable:   {65 * _GIB_KB} kB\n"
            f"SReclaimable:   {17 * _GIB_KB} kB\n"
        )
        data = _collect_with_meminfo(meminfo)
        assert data["mem_total_gb"] == 100.0
        assert data["mem_free_gb"] == 65.0
        assert data["mem_used_gb"] == 35.0

    def test_fallback_includes_sreclaimable(self) -> None:
        """Without MemAvailable, the fallback now adds SReclaimable.

        free = MemFree+Buffers+Cached+SReclaimable = 10+0+40+17 = 67 GiB,
        so used = 33 GiB (the pre-fix code omitted SReclaimable → 50 GiB used).
        """
        meminfo = (
            f"MemTotal:       {100 * _GIB_KB} kB\n"
            f"MemFree:        {10 * _GIB_KB} kB\n"
            f"Buffers:        0 kB\n"
            f"Cached:         {40 * _GIB_KB} kB\n"
            f"SReclaimable:   {17 * _GIB_KB} kB\n"
        )
        data = _collect_with_meminfo(meminfo)
        assert data["mem_total_gb"] == 100.0
        assert data["mem_free_gb"] == 67.0
        assert data["mem_used_gb"] == 33.0
