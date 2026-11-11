"""Each build-desktop.yml leg gets a time cap that fits its build, read from source.

The universal macOS leg builds both per-arch backends and merges two apps into
one, so it runs several times as long as a single-arch leg and gets 90 minutes.
Every other leg keeps 60. The job reads the cap per leg through
``${{ matrix.timeout_minutes || 60 }}``, so a leg without the key falls back to
60, not to GitHub's 360-minute default.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-desktop.yml"

# The one expression shape the job uses: a matrix key with an integer fallback.
_MATRIX_OR_DEFAULT = re.compile(r"\$\{\{ matrix\.(\w+) \|\| (\d+) \}\}")


def _effective_timeout(expr: object, leg: dict) -> int:
    """The minutes GitHub applies to ``leg`` for a job-level ``timeout-minutes``."""
    if isinstance(expr, int):
        return expr
    match = _MATRIX_OR_DEFAULT.fullmatch(str(expr))
    assert match, f"unexpected timeout-minutes expression: {expr!r}"
    return int(leg.get(match.group(1)) or match.group(2))


def test_the_universal_macos_leg_gets_90_minutes_and_the_linux_legs_60() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["build-desktop"]
    legs = job["strategy"]["matrix"]["include"]
    caps = {leg["artifact-name"]: _effective_timeout(job["timeout-minutes"], leg) for leg in legs}
    assert caps == {
        "unsigned-build-darwin-universal": 90,
        "build-linux-x64": 60,
        "build-linux-arm64": 60,
    }
