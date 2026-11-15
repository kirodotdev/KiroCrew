"""One credential plan reads the engine selector once and uses that answer throughout.

The owner's engine PUT publishes a new answer at any moment. A plan that read the
selector again part way (to choose pass 1's matches) could run the legacy passes
with pass 1 narrowed to what the structured detectors leave, though the detectors
never ran: a private-key block then lost its match. The selector here answers one
way on its first read and the other way after, which is that flip at its worst
point, with no thread and no sleep.
"""

from __future__ import annotations

import pytest

from kiro_crew.security.redaction import redact_credentials
from kiro_crew.security.redaction_structured import compat
from kiro_crew.testing.synthetic_credentials import B64, synth


def _pem(seed: int) -> tuple[str, str, str]:
    """A private-key block with one full body line and a short last one."""
    full, last = synth(64, B64, seed), synth(20, B64, seed + 1)
    begin, end = "-----BEGIN RSA " + "PRIVATE KEY-----", "-----END RSA " + "PRIVATE KEY-----"
    return f"{begin}\n{full}\n{last}\n{end}", full, last


def _flipping(first: bool, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Make the selector answer ``first`` once, then the other engine; returns its answers."""
    answers: list[bool] = []

    def selector() -> bool:
        answers.append(first if not answers else not first)
        return answers[-1]

    monkeypatch.setattr(compat, "use_rule_table", selector)
    return answers


@pytest.mark.parametrize(
    "first", [False, True], ids=["legacy-then-structured", "structured-then-legacy"]
)
def test_a_flip_during_one_plan_still_removes_a_private_key(monkeypatch, first):
    text, full, last = _pem(seed=61)
    _flipping(first, monkeypatch)
    out = redact_credentials(f"key:\n{text}\nend")[0]
    assert full not in out and last not in out, out
    assert "PRIVATE KEY" not in out, out


@pytest.mark.parametrize("first", [False, True], ids=["legacy", "structured"])
def test_one_plan_reads_the_selector_once(monkeypatch, first):
    text, _full, _last = _pem(seed=63)
    answers = _flipping(first, monkeypatch)
    redact_credentials(text)
    assert answers == [first]
