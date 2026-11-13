"""``source_providers.issue_search`` string code agrees with the regexes it replaced.

The module once used regular expressions for its validators and its markdown
strip. Each old pattern is kept HERE as an oracle, and every replacement is
checked against it on hand-picked edge and adversarial inputs plus a seeded,
generated corpus, so the rewrite cannot change what a request accepts or what
an excerpt says.
"""

from __future__ import annotations

import random
import re

import pytest

from kiro_crew.dashboard.source_providers import issue_search

# ── The replaced patterns, verbatim ─────────────────────────────────────────

_CURSOR_RE = re.compile(r"[1-9][0-9]{0,5}")
_HEADER_INT_RE = re.compile(r"[0-9]{1,9}")
_GITHUB_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}")
_GITHUB_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
_GITLAB_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]{1,255}")
_GITLAB_LOGIN_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,254}")
_QUOTED_RE = re.compile(r'"[^"]*"')

_HTML_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_FENCE_RE = re.compile(r"^[ \t]*(?:`{3,}|~{3,}).*$", re.MULTILINE)
_IMAGE_RE = re.compile(r"!\[([^\[\]\n]*)\]\([^()\n]*\)")
_LINK_RE = re.compile(r"\[([^\[\]\n]*)\]\([^()\n]*\)")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>\n]{0,200}>")
_LINE_PREFIX_RE = re.compile(
    r"^[ \t]*(?:>[ \t]?)*(?:#{1,6}[ \t]+|[-*+][ \t]+(?:\[[ xX]\][ \t]+)?|\d{1,9}[.)][ \t]+)?",
    re.MULTILINE,
)
_RULE_RE = re.compile(r"^[ \t]*(?:[-*_][ \t]*){3,}$", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"\*\*|__|~~|`")
_SPACE_RE = re.compile(r"\s+")


def _old_strip_markdown(body: str) -> str:
    if not body:
        return ""
    text = _HTML_COMMENT_RE.sub(" ", body)
    text = _FENCE_RE.sub(" ", text)
    text = _IMAGE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _RULE_RE.sub(" ", text)
    text = _LINE_PREFIX_RE.sub("", text)
    return _EMPHASIS_RE.sub("", text).replace("|", " ")


def _old_drop_partial_word(text: str) -> str:
    if not text or text[-1].isspace():
        return text
    match = re.search(r"\s\S*\Z", text)
    return text[: match.start()] if match else ""


def _old_cut_excerpt(text: str) -> str:
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) <= 280:
        return text
    cut = _old_drop_partial_word(text[:281]).rstrip()
    return cut + "…" if cut else ""


# ── The corpus ──────────────────────────────────────────────────────────────

_DIGITS = ["1", "0", "9", "²", "٣", "１", "۵", "߁"]
_SPACES = [" ", "\t", "\n", "\r", "\r\n", "\u00a0", "\u2003", "\x1c", "\x85", "\u200b", "\f"]

_TEXT_EDGES = [
    "",
    " ",
    "\n",
    "\n\n",
    "x" * 400,
    "word " * 120,
    "a" * 281,
    "a " + "b" * 300,
    "x" * 280 + " y",
    "x" * 279 + " y",
    "-----BEGIN **PRIVATE** KEY-----\nMIIE**AA**\n-----END **PRIVATE** KEY-----",
    "-----BEGIN __RSA__ PRIVATE KEY-----",
    "ghp_" + "b" * 16 + "**" + "b" * 20,
    "ghp_" + "b" * 16 + "[](<!--\nfiller\n-->)" + "b" * 20,
    "[a](b(c)d)",
    "[a]((b))",
    "[a](b) [c](d) ![e](f)",
    "![a![b](https://u)",
    "[a[b](https://u)",
    "[a](x(https://u)",
    "![a](x(https://u)",
    "![" * 50 + "a](https://u)",
    "![" * 300,
    "[" * 300,
    "[](" * 200,
    "](" * 200,
    "[x](y" * 100,
    "<!--" * 50,
    "<!-- open",
    "<!-->x-->y",
    "<!--->",
    "a<!--b-->c<!--d",
    "```\ncode\n```",
    "  ```py",
    "\t~~~",
    "``",
    "~~ not a fence",
    "x ```",
    "---",
    "- - -",
    "***",
    "_ _ _",
    "--",
    "-*_",
    " \t---\t ",
    "---\r",
    "----x",
    "# h",
    "###### h",
    "####### h",
    "#h",
    "#\th",
    "> quote",
    ">> twice",
    "> > spaced",
    ">  two spaces # h",
    "> # quoted heading",
    "- item",
    "* item",
    "+ item",
    "-item",
    "- [ ] todo",
    "- [x] done",
    "- [X] done",
    "- [y] not",
    "- [ ]no space",
    "-  [ ]  wide",
    "1. one",
    "123456789. nine",
    "1234567890. ten",
    "1) paren",
    "1.no",
    "٣. arabic",
    "²) super",
    "１. fullwidth",
    "1²) super after a digit",
    "1²٣. mixed digits",
    "٣². arabic then super",
    "  12. indented",
    "<b>bold</b>",
    "</i>",
    "<1>",
    "</ x>",
    "<a" + "b" * 199 + ">",
    "<a" + "b" * 200 + ">",
    "<a" + "b" * 201 + ">",
    "<a\nb>",
    "<a <b>",
    "<" * 300,
    "<a" * 200,
    "**bold** __under__ ~~strike~~ `code`",
    "***",
    "_**_",
    "*`*",
    "~~~~~",
    "_~~_",
    "*" * 301,
    "*_" * 200,
    "`" * 300,
    "a|b||c",
    "line\r\nline\r\n",
    "\u00a0lead\u2003mid\x1ctrail\x85",
    "zero\u200bwidth",
]
_TEXT_TOKENS = [
    "a",
    "b",
    "word",
    *_DIGITS,
    *_SPACES,
    "[",
    "]",
    "(",
    ")",
    "![",
    "](",
    "<",
    ">",
    "/",
    "!",
    "-",
    "*",
    "+",
    "_",
    "~",
    "`",
    "#",
    ".",
    ")",
    "|",
    '"',
    ":",
    "x",
    "X",
    "<!--",
    "-->",
    "```",
    "~~~",
    "**",
    "__",
    "~~",
    "> ",
    "- ",
    "1. ",
    "[ ] ",
    "[x] ",
    "<b>",
    "</a>",
    "[l](u)",
    "![i](u)",
    "ghp_",
]

_LOGIN_EDGES = [
    "",
    "a",
    "A",
    "0",
    "a" * 39,
    "a" * 40,
    "a" * 100,
    "a" * 101,
    "a" * 255,
    "a" * 256,
    "-a",
    "a-",
    "_a",
    "a_b",
    ".a",
    "a.b",
    "..",
    ".",
    "a b",
    "a:b",
    "a\n",
    "\na",
    "a\t",
    "é",
    "aé",
    "ß",
    *_DIGITS,
    "0" * 6,
    "1" * 6,
    "1" * 7,
    "1" * 9,
    "1" * 10,
    "012",
    "100000",
    "999999",
    "1000000",
    "1²",
    "٣",
    "--hostname",
    "@me",
    "a/b",
]
_LOGIN_TOKENS = ["a", "Z", "0", "9", "-", "_", ".", " ", ":", "/", "é", "²", "١", "\n"]

_SEED = 20261111


def _generated(tokens: list[str], count: int, most: int) -> list[str]:
    rng = random.Random(_SEED)
    return ["".join(rng.choices(tokens, k=rng.randint(0, most))) for _ in range(count)]


_TEXTS = _TEXT_EDGES + _generated(_TEXT_TOKENS, 4000, 40)
_VALUES = _LOGIN_EDGES + _generated(_LOGIN_TOKENS, 4000, 12)


# ── Validators ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("new", "old"),
    [
        (issue_search._is_cursor, _CURSOR_RE),
        (issue_search._is_header_int, _HEADER_INT_RE),
        (issue_search._is_github_login, _GITHUB_LOGIN_RE),
        (issue_search._is_github_repo, _GITHUB_REPO_RE),
        (issue_search._is_gitlab_segment, _GITLAB_SEGMENT_RE),
        (issue_search._is_gitlab_login, _GITLAB_LOGIN_RE),
    ],
    ids=["cursor", "header-int", "github-login", "github-repo", "gitlab-segment", "gitlab-login"],
)
def test_validator_agrees_with_its_pattern(new, old) -> None:
    mismatches = [v for v in _VALUES if new(v) != bool(old.fullmatch(v))]
    assert not mismatches, mismatches[:5]


def test_quoted_spans_are_removed_like_the_pattern() -> None:
    values = _VALUES + _TEXTS + ['"a"b"c', '""', '"', 'x"y"z"', '"a b"c', 'repo:"x"']
    mismatches = [v for v in values if issue_search._without_quoted(v) != _QUOTED_RE.sub("", v)]
    assert not mismatches, mismatches[:5]


# ── The markdown strip, pass by pass and whole ──────────────────────────────


@pytest.mark.parametrize(
    ("new", "old"),
    [
        (issue_search._strip_comments, lambda t: _HTML_COMMENT_RE.sub(" ", t)),
        (issue_search._strip_fences, lambda t: _FENCE_RE.sub(" ", t)),
        (
            lambda t: issue_search._strip_bracket_links(t, "!["),
            lambda t: _IMAGE_RE.sub(r"\1", t),
        ),
        (
            lambda t: issue_search._strip_bracket_links(t, "["),
            lambda t: _LINK_RE.sub(r"\1", t),
        ),
        (issue_search._strip_tags, lambda t: _HTML_TAG_RE.sub(" ", t)),
        (
            issue_search._strip_line_marks,
            lambda t: _LINE_PREFIX_RE.sub("", _RULE_RE.sub(" ", t)),
        ),
        (issue_search._strip_emphasis, lambda t: _EMPHASIS_RE.sub("", t)),
        (issue_search.strip_markdown, _old_strip_markdown),
        (issue_search.cut_excerpt, _old_cut_excerpt),
        (issue_search._drop_partial_word, _old_drop_partial_word),
        (issue_search.plain_excerpt, lambda t: _old_cut_excerpt(_old_strip_markdown(t))),
    ],
    ids=[
        "comments",
        "fences",
        "images",
        "links",
        "tags",
        "rules-and-prefixes",
        "emphasis",
        "strip-markdown",
        "cut-excerpt",
        "drop-partial-word",
        "plain-excerpt",
    ],
)
def test_strip_step_agrees_with_its_pattern(new, old) -> None:
    mismatches = [t for t in _TEXTS if new(t) != old(t)]
    assert not mismatches, [m[:120] for m in mismatches[:5]]


@pytest.mark.parametrize(
    "unit",
    ["![", "[", "[a", "[](", "](", "![x](", "<a", "<!--", "**", "*_", "`", "#", "1", "> ", "\n"],
)
def test_long_adversarial_runs_agree(unit) -> None:
    """Long runs of one construct, the inputs a body can be built from to probe
    the strip's worst case."""
    body = unit * (20_000 // len(unit))
    assert issue_search.strip_markdown(body) == _old_strip_markdown(body)
