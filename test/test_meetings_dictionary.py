"""Domain-dictionary tests: matching semantics and hostile-input tolerance.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

``dictionary.toml`` is user-editable AND writable by the agent's own file tools,
so every malformed-input case must degrade to "no corrections" rather than raise
into a request handler.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from meetings_helpers import reset_module_state_fixture  # noqa: F401

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend.domain.dictionary import DomainDictionary


@pytest.fixture
def dictionary(tmp_path: Path) -> DomainDictionary:
    path = tmp_path / "dictionary.toml"
    path.write_text(
        '[[term]]\ncorrect = "DynamoDB"\naliases = ["dynamo db", "dynamo d b"]\n'
        '\n[[term]]\ncorrect = "KiroCrew"\naliases = ["kiro crew", "kiro-crew"]\n'
    )
    d = DomainDictionary()
    d.load(path)
    return d


class TestMatching:
    def test_empty_dictionary_is_identity(self):
        assert DomainDictionary().correct("hello world") == "hello world"

    def test_basic_correction(self, dictionary):
        assert dictionary.correct("we use dynamo db") == "we use DynamoDB"

    def test_case_insensitive(self, dictionary):
        assert dictionary.correct("Dynamo DB is fast") == "DynamoDB is fast"

    def test_multiple_corrections_in_one_line(self, dictionary):
        assert dictionary.correct("kiro crew uses dynamo db") == "KiroCrew uses DynamoDB"

    def test_word_boundaries_respected(self, dictionary):
        # No space, so the "dynamo db" alias must not match inside the word.
        assert dictionary.correct("dynamodb") == "dynamodb"

    def test_longest_alias_wins(self):
        d = DomainDictionary()
        d.load_terms(
            [
                {"correct": "AWS", "aliases": ["a w s"]},
                {"correct": "AWS S3", "aliases": ["a w s s three"]},
            ]
        )
        assert d.correct("use a w s s three") == "use AWS S3"

    def test_empty_text_untouched(self, dictionary):
        assert dictionary.correct("") == ""

    def test_regex_special_alias_is_literal(self):
        d = DomainDictionary()
        d.load_terms([{"correct": "C++", "aliases": ["c plus plus"]}])
        # The replacement is a literal, and an alias with regex metacharacters
        # would blow up the compile if it were not escaped.
        assert d.correct("we write c plus plus") == "we write C++"
        d.load_terms([{"correct": "X", "aliases": ["a.*b"]}])
        assert d.correct("aXXXb") == "aXXXb"


class TestLoading:
    def test_missing_file_clears(self, tmp_path: Path):
        d = DomainDictionary()
        d.load_terms([{"correct": "X", "aliases": ["x"]}])
        d.load(tmp_path / "absent.toml")
        assert d.terms == []
        assert d.correct("x") == "x"

    def test_malformed_toml_degrades(self, tmp_path: Path):
        path = tmp_path / "bad.toml"
        path.write_text("[[term]\ncorrect = broken")
        d = DomainDictionary()
        d.load(path)
        assert d.terms == []

    def test_non_list_term_key_degrades(self, tmp_path: Path):
        path = tmp_path / "odd.toml"
        path.write_text('term = "not a list"\n')
        d = DomainDictionary()
        d.load(path)
        assert d.terms == []

    @pytest.mark.parametrize(
        "entry",
        [
            "not a dict",
            {"correct": 5, "aliases": ["x"]},
            {"correct": "X", "aliases": "not a list"},
            {"correct": "", "aliases": ["x"]},
            {"correct": "X", "aliases": []},
            {"correct": "X", "aliases": [None, 5, "  "]},
            {},
        ],
    )
    def test_bad_entries_dropped(self, entry):
        d = DomainDictionary()
        d.load_terms([entry])
        assert d.terms == []

    def test_term_cap_enforced(self):
        d = DomainDictionary()
        d.load_terms(
            [{"correct": f"T{i}", "aliases": [f"t {i}"]} for i in range(k.MAX_DICTIONARY_TERMS + 50)]
        )
        assert len(d.terms) == k.MAX_DICTIONARY_TERMS


class TestMutation:
    def test_add_term(self):
        d = DomainDictionary()
        d.add_term("DynamoDB", ["dynamo db"])
        assert d.correct("dynamo db") == "DynamoDB"

    def test_add_replaces_case_insensitively(self):
        d = DomainDictionary()
        d.add_term("DynamoDB", ["dynamo db"])
        d.add_term("dynamodb", ["dyno"])
        assert len(d.terms) == 1
        assert d.correct("dyno") == "dynamodb"

    @pytest.mark.parametrize(
        "correct,aliases",
        [("", ["x"]), ("X", []), ("X", ["  "]), ("a" * 200, ["x"]), ("X", ["a" * 200])],
    )
    def test_add_rejects_invalid(self, correct, aliases):
        with pytest.raises(ValueError):
            DomainDictionary().add_term(correct, aliases)

    def test_add_refuses_past_cap(self):
        d = DomainDictionary()
        d.load_terms(
            [{"correct": f"T{i}", "aliases": [f"t {i}"]} for i in range(k.MAX_DICTIONARY_TERMS)]
        )
        with pytest.raises(ValueError):
            d.add_term("One more", ["one more"])

    def test_remove_term(self, dictionary):
        assert dictionary.remove_term("DynamoDB") is True
        assert dictionary.correct("dynamo db") == "dynamo db"
        assert dictionary.correct("kiro crew") == "KiroCrew"

    def test_remove_missing_returns_false(self, dictionary):
        assert dictionary.remove_term("Nope") is False


class TestSerialization:
    def test_roundtrip_through_disk(self, tmp_path: Path):
        path = tmp_path / "d.toml"
        first = DomainDictionary()
        first.add_term("DynamoDB", ["dynamo db"])
        first.save(path)
        second = DomainDictionary()
        second.load(path)
        assert second.as_list() == [{"correct": "DynamoDB", "aliases": ["dynamo db"]}]

    def test_quote_in_term_cannot_inject_a_table(self, tmp_path: Path):
        # A term containing a quote and a "[[term]]" literal must round-trip as
        # ONE term — the JSON-style escaping is what prevents a break-out.
        path = tmp_path / "d.toml"
        first = DomainDictionary()
        first.add_term('He said "go"\n[[term]]\ncorrect = "evil"', ["trigger"])
        first.save(path)
        second = DomainDictionary()
        second.load(path)
        assert len(second.terms) == 1
        assert "evil" not in [c for c, _ in second.terms]

    def test_backslash_in_term_roundtrips(self, tmp_path: Path):
        path = tmp_path / "d.toml"
        first = DomainDictionary()
        first.add_term("C:\\Windows", ["see windows"])
        first.save(path)
        second = DomainDictionary()
        second.load(path)
        assert second.terms[0][0] == "C:\\Windows"
