"""The compaction-capture guide names only tests that exist.

``docs/guides/acp-compaction-capture.md`` is a checklist for landing a harness's
``/compact`` capture, and the tests it names are the part a reader goes looking
for: each one is where a half-done move fails. A test that was renamed or folded
into another keeps its old name in the guide, and the reader then searches for
something that is not there. docs-lint's ``dead-identifier`` check does not look
inside ``test/``, so that drift needs its own pin.

Every ``test_<name>`` and ``Test<Name>`` the guide spells must be a test module
under ``test/``, a ``def`` in one, or a ``class`` in one.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GUIDE = _REPO_ROOT / "docs" / "guides" / "acp-compaction-capture.md"
_TEST_ROOT = _REPO_ROOT / "test"

_NAME = re.compile(r"\b(test_[a-z0-9_]+|Test[A-Z][A-Za-z0-9]+)\b")


def _cited_names() -> set[str]:
    return set(_NAME.findall(_GUIDE.read_text(encoding="utf-8")))


def _known_names() -> set[str]:
    known: set[str] = set()
    definition = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)", re.MULTILINE)
    for path in _TEST_ROOT.rglob("test_*.py"):
        known.add(path.stem)
        known.update(definition.findall(path.read_text(encoding="utf-8")))
    return known


def test_the_guide_cites_at_least_one_test() -> None:
    """Keeps the check below from passing on a guide whose names stopped matching."""
    assert _cited_names()


def test_every_test_the_guide_names_exists() -> None:
    missing = sorted(_cited_names() - _known_names())
    assert not missing, (
        f"{_GUIDE.relative_to(_REPO_ROOT)} names tests that do not exist under test/: "
        f"{missing}. Cite the test that holds the rule today."
    )
