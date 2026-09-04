"""The guidance an agent copies must demonstrate a form that actually gates.

Every place that teaches an agent to arm a babysit loop used to show the subject
as a bare ``PR #123``. Inference deliberately refuses that form, so a loop armed
from the example stayed on the plain timer and every interval spent a turn -- the
saving read as zero while the mechanism worked perfectly. Measured on a live
gateway: of six loops that asked to be gated, five had written a bare number and
only the one that wrote a URL was gated.

These tests assert the shipped text against the real inference function rather
than against a spelling, so they fail if an example is ever reworded back into a
form that cannot select a subject -- including the ``<owner>/<repo>`` placeholder
style, which reads fine to a human and does not infer.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.probes.targets import infer

ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / "src" / "kiro_crew" / "config" / "prompt.md"
BABYSIT_SKILL = (
    ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "babysit" / "SKILL.md"
)

_URL = re.compile(r"https://github\.com/\S+?/pull/\d+")


def _gating_urls(text: str) -> list[str]:
    """Every pull-request URL in ``text`` that inference actually accepts."""
    return [url for url in _URL.findall(text) if infer("Check %s now." % url) is not None]


def test_the_monitor_start_guidance_demonstrates_a_gating_subject() -> None:
    body = PROMPT.read_text(encoding="utf-8")
    start = body.index("**Using monitor_start:**")
    block = body[start : start + 1200]
    assert _gating_urls(block), (
        "the monitor_start guidance in prompt.md must show the subject as a pull-request "
        "URL that inference accepts; a bare 'PR #123' leaves the loop ungated"
    )


def test_the_babysit_example_message_names_its_subject_by_url() -> None:
    body = BABYSIT_SKILL.read_text(encoding="utf-8")
    # Anchor on the Example section: ``monitor_start(`` also appears far above it
    # as the tool's signature in the Overview, which carries no subject at all.
    example = body.index("## Example")
    start = body.index("monitor_start(", example)
    message = body[start : start + 400]
    assert _gating_urls(message), (
        "the babysit skill's worked example must name the pull request by a URL "
        "inference accepts, since the armed message is what agents copy"
    )


def test_a_bare_number_is_still_refused_so_the_ratchet_means_something() -> None:
    # Guards the guard: if inference ever started accepting a bare reference, the
    # two tests above would pass on the old wording and stop protecting anything.
    assert infer("Babysit PR #8184 (kirodotdev/KiroCrew), branch fix/x") is None
    assert infer("Check https://github.com/<owner>/<repo>/pull/123 now.") is None
