"""Source contract: prepare-pr names a Windows route for its mandatory Semgrep scan.

The pinned Semgrep ships no Windows build, so the skill's semgrep step must say
how to run the same scan from Windows, at the SAME pin the SAST job installs.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev"
PREPARE = SKILLS / "kirocrew-prepare-pr" / "SKILL.md"
WORKFLOW = ROOT / ".github" / "workflows" / "code-review.yml"


def _semgrep_step() -> str:
    text = PREPARE.read_text(encoding="utf-8")
    anchor = "**Run the repository's semgrep rules on the diff**"
    start = text.index(anchor)
    return text[start : text.index("\n", start)]


def _workflow_pin() -> str:
    (pin,) = set(re.findall(r"semgrep==([\w.]+)", WORKFLOW.read_text(encoding="utf-8")))
    return pin


def test_semgrep_step_names_a_windows_route() -> None:
    step = _semgrep_step()
    assert "**On native Windows**" in step
    assert "WSL2" in step
    assert "The scan is still required on Windows." in step


def test_windows_route_uses_the_sast_job_pin() -> None:
    step = _semgrep_step()
    pin = _workflow_pin()
    assert set(re.findall(r"semgrep==([\w.]+)", step)) == {pin}
    assert set(re.findall(r"semgrep/semgrep:([\w.]+)", step)) == {pin}
