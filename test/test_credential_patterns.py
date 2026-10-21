"""Tests for the shared home of the credential-pattern spellings.

These replace two source-grep pins that read ``security.py`` as TEXT and
assert a literal appeared in it. With one home there is no drift left to pin, so
what remains here guards the three properties the consolidation actually rests
on: the home stays import-free, both consumers really do read from it, and the
one deliberate divergence stays exactly as wide as intended and no wider.
"""

from __future__ import annotations

import ast
import importlib
import re
import sys
from pathlib import Path

import pytest

from kiro_crew import credential_patterns as cp


class TestImportFreeShape:
    """The home is on the CLI bootstrap path, so it must import nothing."""

    def test_module_has_no_import_node(self) -> None:
        """Parsed as an AST: not one ``import`` node, ``re`` and futures included.

        ``log_redaction`` installs before the heavy modules load. A ``sys.modules``
        delta cannot see a stdlib module some earlier test already imported, and a
        text scan trips over the docstring, so the AST is the exact check.
        """
        tree = ast.parse(open(cp.__file__, encoding="utf-8").read())
        offenders = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        assert offenders == [], f"credential_patterns must import nothing; found: {offenders}"

    def test_fresh_import_pulls_in_no_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fresh import adds only the module itself to ``sys.modules``."""
        import kiro_crew

        mod_name = "kiro_crew.credential_patterns"
        monkeypatch.setattr(kiro_crew, "credential_patterns", sys.modules[mod_name])
        monkeypatch.delitem(sys.modules, mod_name)

        before = set(sys.modules)
        importlib.import_module(mod_name)
        new = set(sys.modules) - before
        assert new == {mod_name}, f"importing the home pulled in: {sorted(new - {mod_name})}"


class TestSharedSpellings:
    """Both consumers compile the shared strings, not their own copies."""

    def test_scrubber_branches_carry_the_shared_spellings(self) -> None:
        """``security.py``'s compiled patterns contain the shared fragments.

        Asserted against the COMPILED pattern, not the source file: a rename or a
        reflow of the concatenation cannot make this pass vacuously, and a
        hand-written copy reintroduced at one of the five AWS sites fails here.
        """
        from kiro_crew import security

        # The presigned-URL validators use the key ID as a structural anchor.
        for name in ("_S3_PRESIGNED_RE", "_CREDENTIAL_RE"):
            pattern = getattr(security, name).pattern
            assert cp.AWS_KEY_ID in pattern, f"{name} no longer carries the shared AWS spelling"
        # No redaction or blocking rule matches a key ID: it is Identifier tier.
        for name in ("_EXFIL_PATTERNS", "_HARD_CREDENTIAL_RE", "_CREDENTIAL_PATTERNS"):
            pattern = getattr(security, name).pattern
            assert cp.AWS_KEY_ID not in pattern, f"{name} matches an AWS access key ID again"

        assert cp.JWT_MULTI_SEGMENT in security._CREDENTIAL_PATTERNS.pattern

    def test_redaction_floor_compiles_the_shared_spellings(self) -> None:
        """``log_redaction``'s patterns are exactly the shared strings."""
        from kiro_crew.log_redaction import _AWS_KEY_ID_RE, _JWT_RE

        assert _AWS_KEY_ID_RE.pattern == cp.AWS_KEY_ID_REDACTION
        assert _JWT_RE.pattern == cp.JWT_MULTI_SEGMENT

    def test_no_module_spells_the_prefix_group_by_hand(self) -> None:
        """No module under ``src/kiro_crew`` writes the prefix group as a literal.

        The point of the consolidation, enforced across the WHOLE tree rather than
        the two original homes: every site that spells a key-ID prefix reads it
        from here, so a copy pasted anywhere reds here instead of becoming a new
        drift pair.

        Matched on the regex-literal form ``(?:AKIA`` so prose that merely mentions
        an ``AKIA...`` key, and ``AWS_KEY_ID_PREFIXES`` itself, do not trip it.
        """
        src_root = Path(cp.__file__).parent
        offenders = [
            str(path.relative_to(src_root))
            for path in sorted(src_root.rglob("*.py"))
            if "_vendor" not in path.parts
            and "(?:AKIA" in path.read_text(encoding="utf-8", errors="replace")
        ]
        assert offenders == [], (
            "these modules spell the AWS key-ID prefix group out again -- import "
            f"AWS_KEY_ID / AWS_KEY_ID_PREFIXES from credential_patterns instead: {offenders}"
        )

    def test_siblings_no_longer_match_a_key_id(self) -> None:
        """No site outside the two homes matches a key ID."""
        from kiro_crew import sel
        from kiro_crew.apps.builtins.pptx_maker.backend import routes as pptx_routes
        from kiro_crew.metrics import schema as metrics_schema

        assert not hasattr(pptx_routes, "_ENCODED_CREDENTIAL_RE")
        assert all(cp.AWS_KEY_ID not in p.pattern for p in metrics_schema._HIGH_ENTROPY_PATTERNS)
        assert not hasattr(sel, "_AWS_KEY_ANYCASE_RE")


class TestDisjointSpellings:
    """The log floor's key ids and the identifier anchor share no prefix."""

    def test_the_two_spellings_match_disjoint_ids(self) -> None:
        """The anchor matches ``AKIA`` / ``ASIA``; the log floor only ``ABIA`` / ``ACCA``."""
        anchor, floor = re.compile(cp.AWS_KEY_ID), re.compile(cp.AWS_KEY_ID_REDACTION)
        body = "0123456789ABCDEF"
        for prefix in ("AKIA", "ASIA"):
            assert anchor.search(prefix + body)
            assert not floor.search(prefix + body), "the log floor redacts a key ID again"
        for prefix in ("ABIA", "ACCA"):
            assert not anchor.search(prefix + body)
            assert floor.search(prefix + body)

    def test_both_are_built_from_the_shared_fragments(self) -> None:
        """Swapping the body class or a prefix group on one side only fails here."""
        assert cp.AWS_KEY_ID == f"(?:{cp.AWS_KEY_ID_PREFIXES}){cp.AWS_KEY_ID_BODY}"
        assert cp.AWS_KEY_ID_REDACTION == (
            f"(?:{cp.AWS_KEY_ID_REDACTION_ONLY_PREFIXES}){cp.AWS_KEY_ID_BODY}"
        )

    def test_both_spellings_reject_a_short_or_lowercase_body(self) -> None:
        """The body stays 16 UPPERCASE alphanumerics on both sides."""
        for spelling in (cp.AWS_KEY_ID, cp.AWS_KEY_ID_REDACTION):
            compiled = re.compile(spelling)
            prefix = spelling[3:7]
            assert not compiled.search(prefix + "0123456789ABCDE")  # 15 chars
            assert not compiled.search(prefix + "abcdefghijklmnop")  # lowercase
