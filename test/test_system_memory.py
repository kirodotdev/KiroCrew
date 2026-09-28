"""Tests for macOS system-memory accounting in dashboard.handlers_system.

Regression coverage for the bug where the dashboard reported far less "used"
memory than macOS Activity Monitor: the old formula treated every inactive page
as free and ignored compressed memory. See ``_macos_memory_gb``.
"""

from __future__ import annotations

from kiro_crew.dashboard.handlers_system import _macos_memory_gb, _parse_vm_stat

GIB = 1024**3
PAGE = 16384  # Apple Silicon page size
PAGES_PER_GIB = GIB // PAGE  # 65536

# A synthetic vm_stat modelled on a 48 GB machine, mirroring the values from
# the reported screenshot (Activity Monitor: App ~24, Wired ~4, Compressed ~4,
# Cached Files ~15, Memory Used ~32). Crucially "Pages inactive" is large (19
# GiB) — the old code counted all of it as free, yielding 28 GB used / 20 free.
_VM_STAT_48GB = f"""Mach Virtual Memory Statistics: (page size of {PAGE} bytes)
Pages free:                                {1 * PAGES_PER_GIB}.
Pages active:                              {23 * PAGES_PER_GIB}.
Pages inactive:                            {19 * PAGES_PER_GIB}.
Pages speculative:                         294.
Pages throttled:                           0.
Pages wired down:                          {4 * PAGES_PER_GIB}.
Pages purgeable:                           {2 * PAGES_PER_GIB}.
Anonymous pages:                           {26 * PAGES_PER_GIB}.
File-backed pages:                         {15 * PAGES_PER_GIB}.
Pages occupied by compressor:              {4 * PAGES_PER_GIB}.
"""

TOTAL_48GB = 48 * GIB


class TestMacosMemoryGb:
    def test_matches_activity_monitor_not_old_formula(self) -> None:
        """used = (anon - purgeable) + wired + compressed = 24 + 4 + 4 = 32."""
        used, free = _macos_memory_gb(TOTAL_48GB, _VM_STAT_48GB)
        assert used == 32.0
        assert free == 16.0

    def test_used_plus_free_equals_total(self) -> None:
        used, free = _macos_memory_gb(TOTAL_48GB, _VM_STAT_48GB)
        assert round(used + free, 1) == 48.0

    def test_diverges_from_buggy_free_plus_inactive(self) -> None:
        """The old formula (free = Pages free + Pages inactive) gave 20 GB free
        / 28 GB used. The fix must NOT reproduce that."""
        used, free = _macos_memory_gb(TOTAL_48GB, _VM_STAT_48GB)
        old_free = 1.0 + 19.0  # Pages free + Pages inactive
        old_used = 48.0 - old_free
        assert (used, free) != (old_used, old_free)
        assert used > old_used  # compressed + dirty-inactive now counted

    def test_compressed_memory_is_counted_as_used(self) -> None:
        without = _VM_STAT_48GB.replace(
            f"Pages occupied by compressor:              {4 * PAGES_PER_GIB}.",
            "Pages occupied by compressor:              0.",
        )
        used_with, _ = _macos_memory_gb(TOTAL_48GB, _VM_STAT_48GB)
        used_without, _ = _macos_memory_gb(TOTAL_48GB, without)
        assert used_with - used_without == 4.0

    def test_legacy_vm_stat_without_anonymous_falls_back_to_active(self) -> None:
        """Older vm_stat lacks 'Anonymous pages' — fall back to active pages."""
        legacy = "\n".join(
            line for line in _VM_STAT_48GB.splitlines() if "Anonymous pages" not in line
        )
        used, free = _macos_memory_gb(TOTAL_48GB, legacy)
        # active(23) + wired(4) + compressed(4) = 31
        assert used == 31.0
        assert free == 17.0

    def test_used_never_exceeds_total(self) -> None:
        """A parse/field mismatch must not report more than physical memory."""
        used, free = _macos_memory_gb(4 * GIB, _VM_STAT_48GB)
        assert used <= 4.0
        assert free >= 0.0


class TestParseVmStat:
    def test_extracts_page_size_and_counts(self) -> None:
        page_size, counts = _parse_vm_stat(_VM_STAT_48GB)
        assert page_size == PAGE
        assert counts["Pages wired down"] == 4 * PAGES_PER_GIB
        assert counts["Anonymous pages"] == 26 * PAGES_PER_GIB

    def test_defaults_page_size_when_header_missing(self) -> None:
        page_size, counts = _parse_vm_stat("Pages free:  100.\n")
        assert page_size == 16384
        assert counts["Pages free"] == 100
