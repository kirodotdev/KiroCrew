"""The dashboard's RecoveryCard detects recovery rows by content prefix.

Recovery continuations are appended as plain ``inject`` rows whose meta is a CSS
class string, so the only signal the frontend has is the literal prefix text.
If a prefix constant changes here without the TSX changing too, the card
silently stops rendering and the raw machine-facing prompt reappears as a
full-width bubble -- a regression with no test failure anywhere else.
"""

from pathlib import Path

from kiro_crew.dashboard.state import (
    REFUSAL_RECOVERY_PREFIX,
    STALE_RECOVERY_PREFIX,
    TOOL_STALL_RECOVERY_PREFIX,
)

_CARD = (
    Path(__file__).resolve().parents[1]
    / "website"
    / "src"
    / "pages"
    / "chat"
    / "RecoveryCard.tsx"
)


def test_recovery_prefixes_present_in_frontend_card() -> None:
    source = _CARD.read_text(encoding="utf-8")
    for prefix in (
        REFUSAL_RECOVERY_PREFIX,
        STALE_RECOVERY_PREFIX,
        TOOL_STALL_RECOVERY_PREFIX,
    ):
        assert prefix in source, f"RecoveryCard.tsx is missing the prefix {prefix!r}"


def test_refusal_body_shape_matches_card_parsing() -> None:
    """The card counts blocked items by a ``- `` bullet and reads the deny
    pattern out of ``Blocked by security policy: <pattern>``. Both come from
    build_refusal_recovery_prompt plus the host gate's reason string, so pin the
    shape the card relies on."""
    from kiro_crew.dashboard.state import build_refusal_recovery_prompt

    body = build_refusal_recovery_prompt(
        [("Running: mypy src/…", "Blocked by security policy: .*env.*grep.*AWS.*")]
    )
    bullets = [ln for ln in body.splitlines() if ln.strip().startswith("- ")]
    assert len(bullets) == 1
    assert "Blocked by security policy: .*env.*grep.*AWS.*" in body
