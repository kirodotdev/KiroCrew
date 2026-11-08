"""Unit tests for scripts/new_test_stability_scope.py.

Tests filter_stability_scope, the pure function that selects which changed
test files the stability gate should repeat.  No git subprocess is invoked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from new_test_stability_scope import filter_stability_scope  # noqa: E402

# ---------------------------------------------------------------------------
# Basic inclusion / exclusion
# ---------------------------------------------------------------------------


def test_leaf_test_file_kept():
    assert filter_stability_scope(["test/test_foo.py"], set()) == ["test/test_foo.py"]


def test_conftest_excluded():
    assert filter_stability_scope(["test/conftest.py"], set()) == []


def test_shared_helper_excluded():
    # source_corpus.py does not match test_*.py
    assert filter_stability_scope(["test/source_corpus.py"], set()) == []


def test_data_file_excluded():
    assert filter_stability_scope(["test/windows-expected-failures.txt"], set()) == []


def test_src_file_excluded():
    assert filter_stability_scope(["src/kiro_crew/session.py"], set()) == []


def test_subdirectory_test_excluded():
    # test/workflows/test_foo.py: name portion is "workflows/test_foo.py",
    # which contains a path separator and is not a bare leaf name.
    assert filter_stability_scope(["test/workflows/test_foo.py"], set()) == []


# ---------------------------------------------------------------------------
# Corpus gate exclusion
# ---------------------------------------------------------------------------


def test_corpus_gate_excluded():
    gates = {"test/test_coverage_omit_contract.py"}
    result = filter_stability_scope(["test/test_coverage_omit_contract.py"], gates)
    assert result == []


def test_non_gate_kept_when_gate_set_non_empty():
    gates = {"test/test_corpus_gate.py"}
    result = filter_stability_scope(["test/test_foo.py"], gates)
    assert result == ["test/test_foo.py"]


# ---------------------------------------------------------------------------
# Order and deduplication (order is preserved, list is the input)
# ---------------------------------------------------------------------------


def test_order_preserved():
    paths = ["test/test_b.py", "test/test_a.py", "test/test_c.py"]
    assert filter_stability_scope(paths, set()) == paths


def test_empty_inputs():
    assert filter_stability_scope([], set()) == []
    assert filter_stability_scope([], {"test/test_gate.py"}) == []


# ---------------------------------------------------------------------------
# Mixed input (src changes + test changes, the non-leaf PR shape)
# ---------------------------------------------------------------------------


def test_mixed_pr_diff_keeps_only_test_files():
    paths = [
        "src/kiro_crew/agent.py",
        "test/test_agent.py",
        "test/conftest.py",
        "pyproject.toml",
    ]
    assert filter_stability_scope(paths, set()) == ["test/test_agent.py"]


@pytest.mark.parametrize(
    "path,expected",
    [
        ("test/test_valid.py", True),
        ("test/test_a_b_c.py", True),
        ("test/conftest.py", False),
        ("test/test_.py", False),  # stem must have at least one char after test_
        ("test/test_foo_bar.py", True),
        ("test/test_foo-bar.py", False),  # hyphens not in _LEAF_NAME
    ],
)
def test_leaf_name_pattern(path: str, expected: bool) -> None:
    result = filter_stability_scope([path], set())
    assert bool(result) == expected
