"""Tests for ``doc_title.document_title``: the title a paper's main document declares.

The clone handler saves this title as a cloned paper's display name. The document
comes from a cloned repository, so anything the parser cannot read gives ``""``
and the paper keeps its folder name.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.builtins.papyrus.backend.doc_title import document_title


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "\\documentclass{article}\n\\title{MACKEREL}\n\\begin{document}\n\\maketitle",
            "MACKEREL",
            id="plain",
        ),
        pytest.param(r"\title{A \textbf{bold} word}", "A bold word", id="nested-braces"),
        pytest.param(
            "\\title{A long title\n  over two lines}",
            "A long title over two lines",
            id="split-across-lines",
        ),
    ],
)
def test_reads_the_title(source: str, expected: str) -> None:
    assert document_title(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("% \\title{Old draft}\n\\title{Final}", "Final", id="commented-out-title"),
        pytest.param(r"\titleformat{\section}{\bf}{}{0pt}{}\title{Real}", "Real", id="look-alike"),
        pytest.param(r"\title{Deep Nets\thanks{Funded by a grant.}}", "Deep Nets", id="thanks"),
        pytest.param(r"\title{First line\\Second line}", "First line Second line", id="line-break"),
        pytest.param(r"\title{Part~I}", "Part I", id="non-breaking-space"),
        pytest.param(
            r"\title{Theory \& Practice at 99\%}", "Theory & Practice at 99%", id="escaped"
        ),
        pytest.param(
            r"\title{Schr\"odinger \emph{revisited}}", "Schrodinger revisited", id="commands"
        ),
    ],
)
def test_turns_latex_into_plain_text(source: str, expected: str) -> None:
    assert document_title(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("\\documentclass{article}\n\\begin{document}\nHello", id="no-title"),
        pytest.param(r"\title{Never closed", id="unclosed-brace"),
        pytest.param("\\begin{document}\n\\title{Too late}", id="after-begin-document"),
        pytest.param("\\begin {document}\n\\title{Too late}", id="after-spaced-begin-document"),
        pytest.param(
            "\\begin% the body starts here\n{document}\n\\title{Too late}",
            id="after-comment-split-begin-document",
        ),
        pytest.param(r"\title[Short]{Full}", id="short-title-form"),
    ],
)
def test_gives_an_empty_string_without_a_readable_title(source: str) -> None:
    assert document_title(source) == ""
