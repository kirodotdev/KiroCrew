"""The TypeScript / JavaScript detectors of ``test_flake_pattern_ratchet.py``.

Line patterns, one per class, over a frontend test file's text; each returns the
1-based line numbers of its findings. The ratchet owns counting and the cases.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_J1 = re.compile(
    r"new Promise(?:<[^>]*>)?\(\s*\(?\s*(\w+)[^)=]*\)?\s*=>\s*\{?\s*setTimeout\(\s*\1\s*,"
    r"\s*([1-9][\d_]*)"
)
_J3 = re.compile(r"\bDate\.now\(\)|\bnew Date\(\s*\)")
_J4 = re.compile(r"\bif\s*\(\s*!?\s*\(?\s*await\b[^;{]*?\.isVisible\(")
_J5 = re.compile(
    r"expect\([^;]*(?:performance\.now\(\)|Date\.now\(\)|\b\w*(?:elapsed|duration|took)\w*\b)[^;]*\)"
    r"\s*\.\s*toBeLessThan(?:OrEqual)?\(\s*\d",
    re.IGNORECASE,
)
_J6 = re.compile(r"\buseFakeTimers\(")
_STALLS = re.compile(r"\bawait\b|\bwaitFor\b|\bfindBy|\bfindAllBy")


def _j_lines(pattern: re.Pattern[str]) -> Callable[[str, str], list[int]]:
    def detect(rel: str, text: str) -> list[int]:
        return [n for n, line in enumerate(text.splitlines(), 1) if pattern.search(line)]

    return detect


def _j1_electron_promise_sleep(rel: str, text: str) -> list[int]:
    """Promise-sleeps in the Electron tests: ``kirocrew.test-promise-sleep`` reads
    only TypeScript under ``website/src``, ``integration`` and ``playwright``."""
    if not rel.startswith("website/electron/"):
        return []
    return _j_lines(_J1)(rel, text)


def _j3_unpinned_date(rel: str, text: str) -> list[int]:
    if "setSystemTime" in text or "mock.timers" in text:
        return []
    return _j_lines(_J3)(rel, text)


def _j4_visible_branch(rel: str, text: str) -> list[int]:
    if not rel.startswith("website/playwright/"):
        return []
    return _j_lines(_J4)(rel, text)


def _restored_by_a_synchronous_finally(lines: list[str], index: int) -> bool:
    """Whether ``useFakeTimers`` on line *index* opens a ``try`` whose ``finally``
    restores real timers with nothing in between that a fake clock could stall."""
    rest = "\n".join(lines[index : index + 200])
    start = rest.find("try {")
    end = rest.find("finally", start)
    if start < 0 or end < 0:
        return False
    between = rest[:start].split("useFakeTimers", 1)[-1].replace("()", "")
    if between.strip(" \t\n;"):
        return False  # the try is not the next statement
    restore = rest.find("useRealTimers", end)
    return restore >= 0 and not _STALLS.search(rest[start:end])


def _j6_unrestored_fake_timers(rel: str, text: str) -> list[int]:
    if ("afterEach" in text and "useRealTimers" in text) or "onTestFinished" in text:
        return []
    lines = text.splitlines()
    return [
        n
        for n, line in enumerate(lines, 1)
        if _J6.search(line) and not _restored_by_a_synchronous_finally(lines, n - 1)
    ]


JS_DETECTORS: dict[str, Callable[[str, str], list[int]]] = {
    "J1-electron-promise-sleep": _j1_electron_promise_sleep,
    "J3-unpinned-date": _j3_unpinned_date,
    "J4-playwright-visible-branch": _j4_visible_branch,
    "J5-duration-upper-bound": _j_lines(_J5),
    "J6-unrestored-fake-timers": _j6_unrestored_fake_timers,
}
