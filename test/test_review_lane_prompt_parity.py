"""Parity and accuracy pins for review-lane prompts that are copied by hand.

The fork review lanes run a trusted static prompt from the default branch, so
they cannot splice the same-repo lane's prompt file at run time. Their copies
are kept on purpose; these tests make a one-sided edit fail instead of drifting.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
REVIEW_PROMPTS = ROOT / ".github" / "review-prompts"


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _steps(workflow: str) -> list[dict]:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    return [step for job in doc["jobs"].values() for step in job.get("steps", [])]


UX_LANES = ("ux-review.yml", "fork-ux-review.yml")


def _ux_prompt(workflow: str) -> str:
    return next(
        step["with"]["prompt"]
        for step in _steps(workflow)
        if "THE UX GATE" in ((step.get("with") or {}).get("prompt") or "")
    )


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return _flat(text[i : text.index(end, i)])


class TestUxLensParity:
    """ux-review.yml owns the shared lens text; the fork lane copies it.
    Lenses 1-11, the lens-12 coverage rule and the lens-13 continuity rule
    have no lane-specific wording, so they must match across both lanes."""

    BLOCKS = (
        ("THE UX GATE", "<the control or state>`."),
        ("13. STATE-TRANSITION CONTINUITY", "- Evidence: static screenshots"),
    )

    def test_shared_lens_blocks_match(self) -> None:
        owner, fork = (_ux_prompt(lane) for lane in UX_LANES)
        for start, end in self.BLOCKS:
            assert _between(owner, start, end) == _between(fork, start, end), start

    def test_recordings_are_only_listed_evidence(self) -> None:
        # pr-attachment-evidence.sh admits only user-attachments URLs, and the
        # committed loop only the two screenshot directories, so a bare media
        # link in the description is never a recording.
        for lane in UX_LANES:
            prompt = _flat(_ux_prompt(lane))
            assert "a link ending in .gif/.mp4/.webm/.mov" not in prompt, lane
            assert "ending in .gif/.mp4/.webm/.mov counts too" not in prompt, lane
            assert "temp-screenshots/ or .github/screenshots/" in prompt, lane
        script = (ROOT / ".github" / "scripts" / "pr-attachment-evidence.sh").read_text(
            encoding="utf-8"
        )
        assert 'allow="https://github\\.com/user-attachments/assets/' in script

    def test_peer_lane_roster_names_every_lane(self) -> None:
        for lane in UX_LANES:
            prompt = _flat(_ux_prompt(lane))
            assert "Three other automated reviewers" not in prompt, lane
            for peer in ("Design Review", "First Principles Review", "Security Scope Review"):
                assert peer in prompt, (lane, peer)

    def test_conventions_point_at_agents_md(self) -> None:
        for lane in UX_LANES:
            prompt = _flat(_ux_prompt(lane))
            assert "CLAUDE.md and website/AGENTS.md hold the conventions" not in prompt
            assert "AGENTS.md (root) and website/AGENTS.md hold the conventions" in prompt

    def test_fork_lane_qualifies_every_blind_read_exit(self) -> None:
        prompt = _flat(_ux_prompt("fork-ux-review.yml"))
        assert "blind-read report (not in this lane)" in prompt
        assert "so this exit never fires here" in prompt
        assert "there is no blind-read report in this lane" in prompt


def test_fork_opus_candidates_rationale_names_the_base_checkout() -> None:
    text = (WORKFLOWS / "fork-opus-review.yml").read_text(encoding="utf-8")
    assert "lives in the PR-HEAD workspace" not in text
    assert "checked-out\n          # trusted base tree" in text


def test_pr_readiness_comments_point_at_real_pins() -> None:
    text = (WORKFLOWS / "pr-readiness.yml").read_text(encoding="utf-8")
    assert "test_ci_surface_tests.py" not in text
    assert "`test/test_ai_review_workflows.py` refuses" in text
    pins = (ROOT / "test" / "test_ai_review_workflows.py").read_text(encoding="utf-8")
    assert "def test_the_job_gate_refuses_a_workflow_run_upstream" in pins
    # The fast-gate job count changes; the comment names the file instead.
    assert "The eleven cheap blocking gates" not in text
    assert "The cheap blocking gates in fast-gate.yml" in text
