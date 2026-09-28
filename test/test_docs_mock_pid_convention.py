"""The mock-pid convention entry must stay true as the kill surface grows.

The mock-pid entry in ``docs/system-specs/common/testing-conventions.md`` does
two things a reader depends on: it enumerates the kill helpers a mock can
reach, and it names two existing spellings so an author picks one rather than
inventing a third. Both claims rot silently.

``scripts/docs_lint.py`` does not cover this. Its ``dead-identifier`` check is
report-only unless ``--strict-identifiers``, and -- more to the point -- it asks
whether a backticked name exists ANYWHERE in the code trees, never whether it
exists at the path the doc attributes it to. A renamed anchor, or an anchor that
moves to another file, passes that check while the convention entry becomes a
dead pointer.

So the pins here are deliberately the two the linter cannot make:

* every public kill/reap helper on ``platform_compat`` is named in the entry,
  DERIVED from that module rather than from a list copied into this file, so an
  eighth helper reddens here instead of leaving the entry quietly incomplete;
* every ``path::symbol`` anchor the entry cites resolves IN THE FILE IT NAMES.

Neither pin needs a mock, a subprocess or a signal: this file reads source text
only, which is the point -- a behavioural check would have to reach the kill path
to observe the defect, and that is the very thing the convention forbids.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "system-specs" / "common" / "testing-conventions.md"
PLATFORM_COMPAT = REPO_ROOT / "src" / "kiro_crew" / "platform_compat.py"

# The sentence that opens the entry. Matched on prose rather than a heading
# because the entry sits inside a shared section.
_ENTRY_OPENER = "A mock subprocess handed to a real kill path must not carry a pid"


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _entry() -> str:
    """The convention paragraph, isolated from the rest of the section."""
    text = _doc_text()
    start = text.find(_ENTRY_OPENER)
    assert start != -1, (
        f"the mock-pid convention entry is gone from {DOC.relative_to(REPO_ROOT)}; "
        "this test and the entry are a pair -- restore it or delete both."
    )
    # The entry ends at the next blank-line-separated block.
    end = text.find("\n\n", start)
    return text[start : end if end != -1 else len(text)]


def _public_kill_helpers() -> set[str]:
    """Every public kill/reap helper on platform_compat, from its own AST.

    Derived, never hand-listed: a hand-listed set in this file would agree with
    a stale doc forever, which is the failure this test exists to catch.
    """
    tree = ast.parse(PLATFORM_COMPAT.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
        and ("kill" in node.name or "reap" in node.name)
    }


class TestTheMockPidConventionEntryStaysTrue:
    def test_the_entry_is_present(self) -> None:
        assert _ENTRY_OPENER in _doc_text()

    def test_the_entry_names_every_public_kill_helper(self) -> None:
        """An eighth kill helper must redden here, not pass unnoticed.

        The entry tells an author which calls put a mock pid in front of a real
        signal. A helper missing from that list is an author's blind spot, and
        nothing else in the repository would report it.
        """
        helpers = _public_kill_helpers()
        assert helpers, "found no kill helpers -- the AST walk has drifted"
        entry = _entry()
        missing = sorted(h for h in helpers if f"`{h}`" not in entry)
        assert not missing, (
            f"{DOC.relative_to(REPO_ROOT)}'s mock-pid entry does not name "
            f"{missing}. platform_compat gained a kill helper; add it to the "
            "entry's list so the next author sees it."
        )

    def test_every_anchor_the_entry_cites_resolves_in_the_file_it_names(self) -> None:
        """A ``path::symbol`` citation must resolve AT that path.

        ``docs_lint``'s dead-identifier check proves only that a name exists
        somewhere in the trees, so it passes on an anchor that has moved to
        another file -- exactly the rot that turns this entry into a dead
        pointer.
        """
        anchors = re.findall(r"`([\w./-]+\.py)::(\w+)`", _entry())
        assert len(anchors) >= 2, (
            "the entry should cite both spellings as path::symbol anchors; " f"found {anchors}"
        )
        for rel, symbol in anchors:
            target = REPO_ROOT / rel
            assert target.is_file(), f"{rel} (cited for {symbol}) is not a file"
            tree = ast.parse(target.read_text(encoding="utf-8"))
            defined: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    defined.add(node.name)
                elif isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            defined.add(t.id)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    defined.add(node.target.id)
            assert symbol in defined, (
                f"the entry cites `{rel}::{symbol}`, but {symbol} is not defined "
                f"in {rel}. The anchor moved or was renamed; repoint the entry."
            )

    def test_the_unallocatable_anchor_is_actually_unallocatable(self) -> None:
        """The spelling the entry recommends must still be above every pid_max.

        Naming an anchor is not enough: if its value drifts down into the
        allocatable range the entry would be recommending the original bug.
        """
        update_provider = REPO_ROOT / "test" / "test_update_provider.py"
        tree = ast.parse(update_provider.read_text(encoding="utf-8"))
        values = [
            node.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int)
            for t in node.targets
            if isinstance(t, ast.Name) and t.id == "_UNALLOCATABLE_PID"
        ]
        assert values, "_UNALLOCATABLE_PID is no longer a module-level int assignment"
        for value in values:
            assert value > 2**32, (
                f"_UNALLOCATABLE_PID is {value}, which a live process can own on "
                "some supported platform; the convention entry points at it."
            )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-n0"]))
