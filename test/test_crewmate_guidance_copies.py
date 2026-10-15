"""The Members page's short-name collision notices give the remedy the docs give.

``src/kiro_crew/docs/crew-members.md`` states the rule: renaming changes only
the name a crewmate shows, never its short name, so a rename cannot clear a
collision. The two notices repeat that rule where the page is not read, so the
test reads the shipped copy itself and fails if a notice drifts back to a fix
that does not work.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
DOC = ROOT / "src" / "kiro_crew" / "docs" / "crew-members.md"
EN = ROOT / "website" / "src" / "i18n" / "locales" / "en.json"


def _members_page_copy() -> dict[str, str]:
    catalog = json.loads(EN.read_text(encoding="utf-8"))
    return catalog["pages"]["membersPage"]


def test_collision_notices_offer_the_remedy_the_page_states() -> None:
    """Both short-name collision notices say delete-and-recreate, as the page does."""
    doc = DOC.read_text(encoding="utf-8")
    assert (
        "never its short name" in doc and "delete the mate that shows this notice" in doc
    ), "the page no longer states the collision remedy these notices are pinned to"
    copy = _members_page_copy()
    # Which crewmate to delete matters: the thread stays bound to its owner (the
    # one `slug_collision` names), so deleting the owner leaves the survivor
    # refused with `member_pin_mismatch`. "Delete one" is the wrong remedy too.
    remedies = {
        "slug_collision": "delete “{{self}}” and create it again",
        "notes_collision": "delete the one whose chat shows a short-name notice",
    }
    for key, remedy in remedies.items():
        text = copy[key]
        assert "Renaming keeps the short name" in text, f"{key} no longer says a rename cannot help"
        assert remedy in text and "create it again" in text, f"{key} lost the remedy"
        assert not re.search(r"\b(Rename|delete) one\b", text), f"{key} offers a fix that fails"
    assert "Keep “{{name}}”" in copy["slug_collision"], "slug_collision no longer names who to keep"
