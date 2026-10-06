"""Ratchet: a test does not judge a descriptor leak by a process-wide census.

``len(os.listdir("/proc/self/fd"))`` before and after, or
``platform_compat.count_open_fds()`` before and after, counts every descriptor in
the xdist worker -- executors, the SEL writer and pool threads open and close
their own on their own schedule, and a freed number is reissued to any of them.
The census also needs ``/proc``, which macOS does not have. Both shapes redden
the macOS shards for every open pull request: ``/proc/self/fd`` under an
``IS_POSIX`` skip raises FileNotFoundError on macOS, and a whole-process
``count_open_fds()`` drifts by a few descriptors on a loaded runner. Counting only
the descriptors that point at the directory under test does neither.

testing-conventions.md ("A process-wide descriptor census is not a leak check")
names the shapes to use instead: spy the primitive the code opens and closes, or
count descriptors resolved to the object under test. The count is exact, so a
removed site records the new number in the same commit.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group("tree_scan_fd_census_ratchet")

_TEST_ROOT = Path(__file__).resolve().parent

# A whole-process count assigned or returned: the before/after shape. A skipif
# probe (`count_open_fds() is None`) and a probe's own unit test are not matched.
_CENSUS = re.compile(
    r"\blen\(\s*os\.listdir\(\s*['\"]/(?:proc/self|dev)/fd['\"]\s*\)\s*\)"
    r"|(?:=|\breturn)\s*(?:platform_compat|pc)\.count_open_fds\(\)"
)

# The probe's own tests exercise count_open_fds itself. The macOS lane-routing
# test quotes these shapes as string data for its own needle, not as a census.
_EXCLUDED_PREFIXES = ("test_platform_compat", "test_macos_platform_tests_gate")

# test_issue_radar_crew_store.py::test_a_directory_at_the_order_files_name_leaks_no_descriptor
_BASELINE_SITES = 1


def _sites() -> list[str]:
    found: list[str] = []
    for path in sorted(_TEST_ROOT.rglob("*.py")):
        if path.name.startswith(_EXCLUDED_PREFIXES) or path == Path(__file__).resolve():
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if _CENSUS.search(line):
                found.append(f"{path.relative_to(_TEST_ROOT)}:{number}: {line.strip()}")
    return found


def test_the_needle_matches_every_shipped_shape() -> None:
    shipped = (
        'before = len(os.listdir("/proc/self/fd"))',
        "after = platform_compat.count_open_fds()",
        "return platform_compat.count_open_fds() or 0",
        "return len(os.listdir('/dev/fd'))",
    )
    assert all(_CENSUS.search(line) for line in shipped)
    legit = (
        "not platform_compat.IS_POSIX or platform_compat.count_open_fds() is None,",
        "fds = [int(name) for name in os.listdir(fd_dir) if name.isdigit()]",
        'target = os.readlink(f"/proc/self/fd/{fd}")',
        "before = _descriptors_pointing_at(path.parent)",
    )
    assert not any(_CENSUS.search(line) for line in legit)


def test_process_wide_descriptor_census_sites_match_the_recorded_count() -> None:
    sites = _sites()
    assert len(sites) == _BASELINE_SITES, (
        f"{len(sites)} test site(s) judge a descriptor leak by a process-wide census "
        f"(recorded: {_BASELINE_SITES}). A whole-process count drifts under xdist and "
        "needs /proc, which macOS lacks. Count descriptors resolved to the object under "
        "test, or spy the primitive the code opens and closes (testing-conventions.md, "
        "'A process-wide descriptor census is not a leak check'). If you REMOVED a site, "
        "set _BASELINE_SITES to the new number.\n" + "\n".join(sites)
    )
