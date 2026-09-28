"""The dashboard re-encodes this module's tier semantics, so pin the two together.

`McpManagement.tsx` maps every ``Strength`` to a label and names the subset that
argues against sharing. Enforcement of a verdict is deliberately unwired, so that
display is the only thing standing between an operator and a shared backend the
evidence rejects -- and a tier this module gains without a matching frontend entry
does not fail loudly. It falls through to the "not measured" label and the row
silently stops being flagged, which is the dangerous direction.

Nothing at runtime can catch that: the frontend receives a plain string. So the
coupling is held here, where the enum lives.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.mcp_gateway.shareability import Strength

_VIEW = (
    Path(__file__).resolve().parents[1]
    / "website"
    / "src"
    / "pages"
    / "settings"
    / "McpManagement.tsx"
)


def _source() -> str:
    return _VIEW.read_text(encoding="utf-8")


def _block(name: str, text: str) -> str:
    """The literal body of a named map or set in the view, for membership checks."""
    start = text.index(name)
    return text[start : text.index("}", start) if "}" in text[start:] else len(text)]


def test_every_strength_tier_has_a_frontend_label() -> None:
    text = _source()
    block = _block("STRENGTH_LABEL_KEY", text)
    missing = [s.value for s in Strength if f"  {s.value}:" not in block]
    assert not missing, (
        f"{_VIEW.name} has no label for {missing}. An unmapped tier renders as "
        '"not measured", so the row silently stops being flagged.'
    )


def test_the_contrary_set_names_only_real_tiers() -> None:
    # A typo or a renamed tier here is invisible at runtime: the Set simply never
    # matches, and every flagged row goes quiet.
    text = _source()
    line = next(ln for ln in text.splitlines() if "CONTRARY_STRENGTHS" in ln and "Set" in ln)
    named = set(re.findall(r"'([a-z_]+)'", line))
    known = {s.value for s in Strength}
    assert named <= known, f"unknown tier(s) in CONTRARY_STRENGTHS: {sorted(named - known)}"
    # The two tiers that carry evidence AGAINST sharing, as opposed to merely not
    # endorsing it. `unknown` and `no_objection` are absences; `declared` is the
    # positive case.
    assert named == {Strength.REFUTED.value, Strength.DISQUALIFIED.value}, (
        "the frontend's contrary set drifted from the tiers this module treats as "
        f"evidence against sharing: {sorted(named)}"
    )
