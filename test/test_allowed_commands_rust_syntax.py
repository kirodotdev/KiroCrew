"""kiro-cli ``allowedCommands`` entries in Rust's regex syntax, read as Rust reads them.

kiro-cli compiles each entry with Rust's ``regex`` crate and runs a command one
matches whole without a prompt. Kiro Crew answers the same entries at its own gate
on KAS and claude (:mod:`kiro_crew.platform.allowed_commands`), so an entry it
cannot read is a command kiro-cli runs and Kiro Crew refuses under
``denyByDefault``, and an entry it reads wider than Rust is a command Kiro Crew runs
and kiro-cli refuses.

The tables are Rust's own answers, taken with ripgrep 15.2, which matches with the
same crate: ``rg --no-config --regex-size-limit 10M -x -e <entry>`` over the
command (``-x`` anchors the entry at both ends, as kiro-cli does; 10 MiB is the
crate's default size limit).
"""

from __future__ import annotations

import pytest

from kiro_crew.platform import allowed_commands
from kiro_crew.platform.allowed_commands import denied_match, matches, parse


def refusal_reason(entry: object) -> str | None:
    """:func:`allowed_commands.refusal_reason`, looked up when called, so a reader
    without it fails each test that asks rather than the whole file."""
    return allowed_commands.refusal_reason(entry)


#: (entry, command, whether Rust matches the command whole).
_RUST = [
    ("^ls [[:alnum:]]+$", "ls abc1", True),
    ("^ls [[:alnum:]]+$", "ls a-b", False),
    ("^sleep \\d+$", "sleep 30", True),
    ("^sleep \\d+$", "sleep 3x", False),
    ("^mkdir \\w+$", "mkdir build_1", True),
    ("^mkdir \\w+$", "mkdir a.b", False),
    ("^echo \\bhi\\b$", "echo hi", True),
    ("^echo \\bhi\\b$", "echo hix", False),
    ("^echo a{2}$", "echo aa", True),
    ("^echo a{2}$", "echo aaa", False),
    ("^echo a{2}$", "echo a", False),
    ("^cat .+?\\.txt$", "cat notes.txt", True),
    ("^cat .+?\\.txt$", "cat notes.md", False),
    ("(?i)^whoami$", "WhoAmI", True),
    ("(?i)^whoami$", "whoami x", False),
    ("[[:alpha:]]+", "aZ", True),
    ("[[:alpha:]]", "1", False),
    ("[[:alpha:]]", "_", False),
    ("[[:digit:]]+", "059", True),
    ("[[:digit:]]", "a", False),
    ("[[:upper:]]", "A", True),
    ("[[:upper:]]", "a", False),
    ("[[:lower:]]", "a", True),
    ("[[:lower:]]", "A", False),
    ("a[[:space:]]b", "a b", True),
    ("a[[:blank:]]b", "a b", True),
    ("[[:punct:]]+", "!/:@[`{~", True),
    ("[[:punct:]]", "a", False),
    ("[[:xdigit:]]+", "09afAF", True),
    ("[[:xdigit:]]", "g", False),
    ("[[:word:]]+", "a_1", True),
    ("[[:word:]]", "-", False),
    ("[[:graph:]]", "~", True),
    ("[[:graph:]]", " ", False),
    ("[[:print:]]", " ", True),
    ("[[:ascii:]]", "a", True),
    ("[[:ascii:]]", "\u00e9", False),
    ("[[:cntrl:]]", "a", False),
    ("[[:^alpha:]]", "1", True),
    ("[[:^alpha:]]", "a", False),
    ("[[:^alpha:]]", "\u00e9", True),
    ("[^[:alpha:]]", "1", True),
    ("[^[:alpha:]]", "a", False),
    ("[[:alpha:]_-]+", "a_-b", True),
    ("[[:digit:][:upper:]]+", "A1", True),
    ("[[:digit:][:upper:]]+", "a", False),
    ("[[:alpha:]-]+", "a-b", True),
    ("\\d+", "123", True),
    ("\\d+", "12a", False),
    ("\\D+", "ab", True),
    ("\\D", "1", False),
    ("\\w+", "a_1", True),
    ("\\w+", "a-1", False),
    ("\\W", "-", True),
    ("\\W", "a", False),
    ("a\\sb", "a b", True),
    ("\\S+", "ab", True),
    ("\\S+", "a b", False),
    ("[\\d.]+", "1.2", True),
    ("[^\\d]+", "ab", True),
    ("[^\\d]+", "a1", False),
    ("[\\w-]+", "a-b_c", True),
    ("[^\\W]", "a", True),
    ("[^\\W]", "-", False),
    ("[^\\s]+", "ab", True),
    ("[\\D]", "a", True),
    ("\\ba\\b.*", "a b", True),
    ("\\ba\\b.*", "ab", False),
    ("x\\B.*", "xy", True),
    ("x\\B.*", "x y", False),
    (".*\\bfoo\\b.*", "a foo b", True),
    (".*\\bfoo\\b.*", "afoo", False),
    ("a\\b", "a", True),
    ("a\\B", "a", False),
    ("ls$x", "ls", False),
    ("ls$x", "lsx", False),
    ("a^", "a", False),
    ("\\Aa\\z", "a", True),
    ("^^a$$", "a", True),
    ("a$|b", "a", True),
    ("x(^|y)", "xy", True),
    ("(^|x)a", "a", True),
    ("^*ls", "ls", True),
    ("\\b+ls", "ls", True),
    ("(?:^)*ls", "ls", True),
    ("a{2,3}", "a", False),
    ("a{2,3}", "aa", True),
    ("a{2,3}", "aaa", True),
    ("a{2,3}", "aaaa", False),
    ("a{2,}", "a", False),
    ("a{2,}", "aa", True),
    ("a{2,}", "aaaaa", True),
    ("a{0}b", "b", True),
    ("a{0}b", "ab", False),
    ("(ab){2}", "abab", True),
    ("(ab){2}", "ab", False),
    ("a{ 2 , 3 }", "aa", True),
    ("a{2 ,3}", "aaa", True),
    ("[0-9]{1,3}", "123", True),
    ("[0-9]{1,3}", "1234", False),
    ("a{2}{3}", "aaaaaa", True),
    ("a{2}{3}", "aaaa", False),
    ("(a|bc){1,2}", "bca", True),
    ("(a|bc){1,2}", "a", True),
    ("(a|bc){1,2}", "abcbc", False),
    ("a{002}", "aa", True),
    (".{0,5}x", "abcdex", True),
    (".{0,5}x", "abcdefx", False),
    ("a+?", "aaa", True),
    ("a*?b", "aab", True),
    ("a??b", "b", True),
    ("a??b", "ab", True),
    ("a{1,2}?b", "aab", True),
    ("a{1,2}?b", "aaab", False),
    ("a**", "aaa", True),
    ("a*+", "aa", True),
    ("(a+)?", "", True),
    ("a+*b", "b", True),
    ("a???", "a", True),
    ("(?P<n>ab)+", "abab", True),
    ("(?<n>x)y", "xy", True),
    ("(?P<a.b>x)", "x", True),
    ("(?<_x>x)", "x", True),
    ("(?i)git status", "GIT Status", True),
    ("(?i:git) status", "GIT status", True),
    ("(?i:git) status", "git STATUS", False),
    ("(?i)ab(?-i)c", "ABc", True),
    ("(?i)ab(?-i)c", "ABC", False),
    ("x(?i)a|b", "B", True),
    ("(?:x(?i)a|b)c", "Bc", True),
    ("(?:x(?i)a|b)c", "bC", False),
    ("(?i)[a-c]+", "AbC", True),
    ("(?i)[^a]", "A", False),
    ("(?i)[^a]", "b", True),
    ("(?i)[[:lower:]]", "Q", True),
    ("(?i)[[:^alpha:]]", "k", False),
    ("(?i)[[:^alpha:]]", "1", True),
    ("(?i)[^[:^alpha:]]", "k", True),
    ("(?i)[^[:^alpha:]]", "s", True),
    ("(?i)\\W", "k", False),
    ("(?i)[^\\W]", "k", True),
    ("(?i)[^\\D]", "k", False),
    ("(?i)\\D", "k", True),
    ("(?i)[^\\S]", "s", False),
    ("(?i)k", "K", True),
    ("(?i)s+", "sSs", True),
    ("(?i)[[:^lower:]]", "a", False),
    ("(?i)[^[:upper:]]", "a", False),
    ("(?i)[^[:upper:]]", "1", True),
    ("(?m)^ls$", "ls", True),
    ("(?s)a.c", "abc", True),
    ("(?U)a+", "aaa", True),
    ("(?is)A.", "ab", True),
    ("(?i-s)A", "a", True),
    ("(?-i)a", "A", False),
    ("(?i)(?-i)a", "A", False),
    ("(?im)A", "a", True),
    ("(?i)", "", True),
    ("(?i)[k]", "K", True),
    ("(?i)[x-z]", "Y", True),
    ("a]", "a]", True),
    ("a}", "a}", True),
    ("a\\ b", "a b", True),
    ("[\\ ]", " ", True),
    ("\\-x", "-x", True),
    ("(?P<a>x)(?P<b>y)", "xy", True),
    ("a{1,}", "a", True),
    ("a{ 1 }", "a", True),
    ("a{1 , 2}", "aa", True),
    ("ls [\u00e0-\u00ff]+", "ls \u00e9", True),
    ("ls [^\u00e0-\u00ff]+", "ls \u00e9", False),
    ("ls [^\u00e0-\u00ff]+", "ls e", True),
    ("touch marker(a{4294967295}){0}", "touch marker", True),
]

#: Entries Rust refuses to compile, so kiro-cli allows nothing by them.
_RUST_REFUSES = [
    "(?P<a>x)(?P<a>y)",
    "(?P<a>x)|(?<a>y)",
    "a{1, }",
    "a{,2}",
    "a{ }",
    "a{1,x}",
    "touch marker(a{4294967296}){0}",
    "touch marker(a{99999999999999999999}){0}",
    "touch marker(a{0,4294967296}){0}",
    "touch marker(a{4294967296,}){0}",
]


@pytest.mark.parametrize(("entry", "command", "rust"), _RUST)
def test_an_entry_matches_exactly_what_rust_matches(entry: str, command: str, rust: bool) -> None:
    parsed = parse(entry)
    assert parsed is not None, refusal_reason(entry)
    assert matches(parsed, command) is rust


@pytest.mark.parametrize("entry", _RUST_REFUSES)
def test_an_entry_rust_refuses_to_compile_allows_nothing(entry: str) -> None:
    assert parse(entry) is None
    assert refusal_reason(entry)


def _nested(groups: int, repeats: int) -> str:
    """*groups* nested groups, each and the atom inside carrying *repeats* repeats."""
    return "(x" * groups + "a" + "*" * repeats + (")" + "*" * repeats) * groups


def test_an_entry_rust_finds_too_large_or_too_deep_is_refused() -> None:
    """Rust compiles ``\\w{209}`` and refuses ``\\w{250}`` past its 10 MiB size
    limit, and refuses ``_nested(28, 8)`` as nested past 250. Kiro Crew stops short
    of both, never past them."""
    assert parse(r"\w{96}") is not None
    assert parse(r"[\w./-]{1,64}") is not None
    assert parse(r"\d{300}") is not None
    for too_large in (r"\w{97}", r"\w{250}", r"\w{1,300}", r"\D{400}"):
        assert "too large" in (refusal_reason(too_large) or ""), too_large
    assert parse(_nested(32, 2)) is not None
    assert parse(_nested(28, 8)) is None
    assert parse(_nested(32, 3)) is None


@pytest.mark.parametrize(
    ("entry", "command"),
    [
        (r"^ls \w+$", "ls \u00e9"),
        (r"^sleep \d+$", "sleep \u0663"),
        (r"^echo \bhi\b.*$", "echo hi \u00e9"),
        ("(?i)^k$", "\u212a"),
    ],
)
def test_a_unicode_class_matches_no_non_ascii_command(entry: str, command: str) -> None:
    """Rust reads ``\\w``, ``\\d``, ``\\b`` and ``(?i)`` by Unicode and matches each
    of these; Kiro Crew reads them by ASCII, so such an entry grants an all-ASCII
    command exactly as Rust does and nothing else."""
    parsed = parse(entry)
    assert parsed is not None and parsed.ascii_only
    assert not matches(parsed, command)


def test_a_deny_read_by_ascii_is_unknown_for_a_non_ascii_command() -> None:
    """A deny must never be read narrower than kiro-cli reads it, so a command the
    ASCII reading cannot judge grants nothing."""
    assert denied_match([r"^rm \w+$"], "rm \u00e9") is None
    assert denied_match([r"^rm \w+$"], "rm x") is True
    assert denied_match([r"^make clean\b"], "make clean now", word_bounded=True) is True
    assert denied_match([r"^make clean\b"], "make cleanup", word_bounded=True) is False
    assert denied_match([r"^make (?i)CLEAN"], "make clean x", word_bounded=True) is True


@pytest.mark.parametrize("unjudged", [r"make \w+", r"make \p{L}"])
def test_a_deny_that_cannot_judge_never_hides_one_that_matches(unjudged: str) -> None:
    """A deny entry that cannot judge a command (ASCII-read against a non-ASCII
    command, or outside the syntax read) leaves the answer unknown only when no
    other entry matches: one that does still names the command, in either order."""
    command = "npm publish ./caf\u00e9"
    for word_bounded in (False, True):
        for entries in ([r"npm publish( .*)?", unjudged], [unjudged, r"npm publish( .*)?"]):
            assert denied_match(entries, command, word_bounded=word_bounded) is True
            assert denied_match(entries, "npm test \u00e9", word_bounded=word_bounded) is None
        assert denied_match([r"npm publish( .*)?"], "npm test \u00e9") is False


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (r"ls \p{L}", "the escape \\p"),
        ("(?x)ls", "the x flag"),
        ("ls (?=a)", "a look-around"),
        ("(?P<a>x)(?P<a>y)", "two groups with one name"),
        ("ls a{1, }", "a malformed counted repeat"),
        ("ls (a{4294967296}){0}", "a counted repeat past 4294967295"),
        ("ls a***", "more than 2 repeats on one atom"),
        ("ls (a", "an unclosed group"),
    ],
)
def test_a_refused_entry_says_why(entry: str, reason: str) -> None:
    assert refusal_reason(entry) == reason
    assert refusal_reason("git status") is None


def test_a_refusal_names_the_entries_it_cannot_read() -> None:
    note = allowed_commands.unreadable_note(["git status", r"ls \p{L}", "(?x)a"])
    assert r"ls \p{L} (the escape \p)" in note
    assert "(?x)a (the x flag)" in note
    assert "git status" not in note
    assert allowed_commands.unreadable_note(["git status", r"^sleep \d+$"]) == ""
    many = allowed_commands.unreadable_note([f"(?x){i}" for i in range(5)])
    assert "(?x)2 (the x flag); and 2 more" in many
