"""Every doc that enumerates ``DIRECTIVE_TOOLS`` must name every member.

Four docs carry that enumeration, one of them bundled into the wheel and read by
users. Nothing reported drift between them and the set: ``Docs Lint`` checks index
links only, so adding a directive updated the code and silently left a doc naming a
short list, and the next reader of that list concluded the new directive was not a
session directive at all.

Membership is read off ``DIRECTIVE_TOOLS`` rather than re-spelled here, so a member
added later is required at every surface without touching this file. The bundled copy
is resolved through the installed package, not by repo path, so the assertion covers
the copy that actually ships.
"""

import pathlib

import pytest

from kiro_crew.session_directive import DIRECTIVE_TOOLS

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_COUNT_WORDS = {
    6: "Six",
    7: "Seven",
    8: "Eight",
    9: "Nine",
    10: "Ten",
    11: "Eleven",
    12: "Twelve",
}


def _bundled_transport_doc() -> pathlib.Path:
    import kiro_crew

    return pathlib.Path(kiro_crew.__file__).resolve().parent / "docs" / "messaging-transport.md"


def _enumerating_docs() -> dict[str, pathlib.Path]:
    return {
        "session spec": _REPO_ROOT / "docs" / "system-specs" / "modules" / "session.md",
        "mcp architecture": _REPO_ROOT / "docs" / "architecture" / "mcp.md",
        "bundled transport doc": _bundled_transport_doc(),
    }


class TestDirectiveToolsDocParity:
    def test_every_enumerating_doc_names_every_directive_tool(self) -> None:
        missing: dict[str, list[str]] = {}
        for label, path in _enumerating_docs().items():
            assert path.is_file(), f"{label} not found at {path}"
            text = path.read_text(encoding="utf-8")
            absent = sorted(name for name in DIRECTIVE_TOOLS if name not in text)
            if absent:
                missing[label] = absent
        assert not missing, f"directive(s) absent from doc(s): {missing}"

    def test_the_spec_count_word_matches_the_member_count(self) -> None:
        total = len(DIRECTIVE_TOOLS)
        if total not in _COUNT_WORDS:
            pytest.fail(
                f"DIRECTIVE_TOOLS has {total} members, outside the count words this "
                f"test can check ({sorted(_COUNT_WORDS)}) — extend _COUNT_WORDS"
            )
        spec = (_REPO_ROOT / "docs" / "system-specs" / "modules" / "session.md").read_text(
            encoding="utf-8"
        )
        expected = f"{_COUNT_WORDS[total]} session-bound MCP tools"
        assert expected in spec, f"session spec does not open its list with {expected!r}"

        stale = [
            f"{word} session-bound MCP tools"
            for count, word in _COUNT_WORDS.items()
            if count != total
        ]
        present = sorted(phrase for phrase in stale if phrase in spec)
        assert not present, f"session spec still carries a stale count: {present}"
