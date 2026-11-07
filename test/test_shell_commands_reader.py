"""The shared quote-aware command reader (``security/shell_commands.py``).

Two halves: what the reader yields per command (words with quote state and raw
offsets, redirections including leading ones, the live separator), and parity --
the ``rm`` floor and the ``-d @`` carve-out moved onto the reader give the same
answer as the readings they replaced, kept below as the oracle.
"""

from __future__ import annotations

import hashlib
import random
import re

import pytest

from kiro_crew.security import exfil_bash, rm_floor
from kiro_crew.security import shell_normalizer as _shell
from kiro_crew.security.deny_gate import is_denied
from kiro_crew.security.shell_commands import SUBST, read_commands


def _shape(text: str) -> "list[tuple[str, list[str], list[tuple[str, str | None]]]]":
    """(separator, argv raws, (redirect raw, target raw)) per command."""
    return [
        (
            cmd.separator,
            [word.raw for word in cmd.argv],
            [(op.raw, target.raw if target else None) for op, target in cmd.redirects],
        )
        for cmd in read_commands(text)
    ]


class TestReader:
    def test_a_quoted_separator_or_redirect_stays_one_word(self):
        assert _shape("echo ';' '>' \"a|b\" x\\;y") == [
            ("", ["echo", "';'", "'>'", '"a|b"', "x\\;y"], []),
        ]

    def test_a_quoted_semicolon_redirect_target_is_the_target_not_a_cut(self):
        assert _shape("bash -c 2> ';' 'wipe'") == [
            ("", ["bash", "-c", "'wipe'"], [("2>", "';'")]),
        ]

    @pytest.mark.parametrize(
        ("text", "separator"),
        [("a&&b", "&&"), ("a&b", "&"), ("a||b", "||"), ("a|b", "|"), ("a;b", ";"), ("a\nb", "\n")],
    )
    def test_a_glued_control_operator_ends_the_command(self, text, separator):
        assert _shape(text) == [(separator, ["a"], []), ("", ["b"], [])]

    def test_a_leading_redirect_is_kept_and_its_target_is_not_argv(self):
        assert _shape("2>/dev/null gh api x") == [("", ["gh", "api", "x"], [("2>/dev/null", None)])]
        assert _shape("2> err {fd}>out gh api") == [
            ("", ["gh", "api"], [("2>", "err"), ("{fd}>out", None)]),
        ]

    def test_an_fd_dup_is_part_of_the_redirect_not_a_background_cut(self):
        assert _shape("cmd 2>&1 >> log") == [("", ["cmd"], [("2>&1", None), (">>", "log")])]

    def test_a_digit_glued_to_a_path_is_not_an_fd_prefix(self):
        assert _shape("cat /tmp/2>x") == [("", ["cat", "/tmp/2"], [(">x", None)])]

    def test_a_heredoc_stops_the_read(self):
        commands = read_commands("cat <<EOF\nbody; more\nEOF")
        assert [c.separator for c in commands] == ["<<"]

    def test_a_comment_is_skipped_to_its_newline(self):
        assert _shape("a # b; c\nd") == [("\n", ["a"], []), ("", ["d"], [])]

    def test_a_double_quoted_substitution_is_read_first_and_left_as_one_step(self):
        commands = read_commands('echo "x$(inner a;b)y" z')
        assert [[w.raw for w in c.argv] for c in commands] == [
            ["inner", "a"],
            ["b"],
            ["echo", f'"x{SUBST}y"', "z"],
        ]

    def test_offsets_and_quote_state_index_the_raw_text(self):
        text = "  ls 'a b'\\ c"
        (cmd,) = read_commands(text)
        word = cmd.words[1]
        assert text[word.start : word.end] == word.raw == "'a b'\\ c"
        # ' a <sp> b ' then the escape pair "\ " then c: the opening quote and the
        # bare c are live syntax, everything between them is quoted or escaped
        assert word.quoted == (False, True, True, True, True, True, False)


class TestSubstitutionWords:
    """A live ``$(…)``, backtick or ``<(…)`` is one word: the outer argv runs on past it."""

    @pytest.mark.parametrize(
        ("text", "inner", "outer"),
        [
            (
                "gh release create $(git describe --tags) --notes-file n.md",
                ["git", "describe", "--tags"],
                ["gh", "release", "create", SUBST, "--notes-file", "n.md"],
            ),
            (
                "gh api `echo /x` --input s.json",
                ["echo", "/x"],
                ["gh", "api", SUBST, "--input", "s.json"],
            ),
            (
                "gh api <(echo /x) --field k=@s",
                ["echo", "/x"],
                ["gh", "api", SUBST, "--field", "k=@s"],
            ),
            (
                "gh release create $(echo v1) -F n",
                ["echo", "v1"],
                ["gh", "release", "create", SUBST, "-F", "n"],
            ),
            ("cat a>(tee b) c", ["tee", "b"], ["cat", f"a{SUBST}", "c"]),
            ("x$(a;b)y z", ["b"], [f"x{SUBST}y", "z"]),
        ],
    )
    def test_the_words_after_a_substitution_stay_in_its_command(self, text, inner, outer):
        argvs = [[w.raw for w in c.argv] for c in read_commands(text)]
        assert argvs[-1] == outer
        assert inner in argvs[:-1]  # the body is still read as commands of its own

    def test_a_separator_after_the_substitution_still_cuts(self):
        assert [[w.raw for w in c.argv] for c in read_commands("ls $(pwd) ; rm x")] == [
            ["pwd"],
            ["ls", SUBST],
            ["rm", "x"],
        ]

    def test_a_subshell_paren_and_an_escaped_dollar_still_cut(self):
        assert [c.separator for c in read_commands("(a) b")] == ["(", ")", ""]
        assert [[w.raw for w in c.argv] for c in read_commands("echo \\$(a) b")][-1] == ["b"]

    def test_a_wipe_behind_a_substitution_operand_is_refused(self):
        for command in (_WIPE + "~/$(true)", _WIPE + "/`true`", "echo $(date) && " + _WIPE + "~"):
            assert is_denied(command) is not None, repr(command)


# -- Parity oracles: the readings the reader replaced, unchanged ------------------

_ORACLE_CUT_CHARS = frozenset(";&|\n`()")


def _oracle_segments(command: str, track: bool = True) -> "list[str]":
    out: list[str] = []
    seg: list[str] = []
    in_redirect = False
    expansion: list[str] = []
    pos, state = 0, 0
    while pos < len(command):
        text, resume, resume_state = command[pos:], len(command), 0
        dollars = 0
        for step in _shell._iter_shell_chars(text, state):
            dollars = dollars + 1 if step.active and step.char == "$" else 0
            live_syntax = step.active and not expansion
            if live_syntax and text.startswith("<<", step.offset):
                break
            if live_syntax and step.char == "#" and _shell._opens_comment(text, step.offset):
                newline = text.find("\n", step.offset)
                resume = len(command) if newline < 0 else pos + newline
                break
            if step.state == 2 and text.startswith("$(", step.offset):
                body = text[step.offset + 2 :]
                close, proven = _shell._matching_close_paren(body, 0)
                out.extend(_oracle_segments(body[: close - 1 if proven else close], track))
                seg.append(SUBST)
                in_redirect = False
                resume, resume_state = pos + step.offset + 2 + close, 2
                break
            if step.state == 2 and step.char == "`" and len(step.text) == 1:
                closer = _shell._backtick_closer(text, step.offset + 1)
                closed = closer != -1
                end = closer + 1 if closed else len(text)
                out.extend(_oracle_segments(text[step.offset + 1 : end - closed], track))
                seg.append(SUBST)
                in_redirect = False
                resume, resume_state = pos + end, 2
                break
            if track and dollars % 2 and text.startswith(("${", "$["), step.offset):
                expansion.append("}" if text[step.offset + 1] == "{" else "]")
            elif step.active and expansion and step.char == expansion[-1]:
                expansion.pop()
            if step.active and in_redirect and step.char == "&":
                seg.append(step.text)
            elif step.active and step.char in _ORACLE_CUT_CHARS:
                out.append("".join(seg))
                seg = []
                in_redirect = False
            elif step.active and step.char in "<>":
                if in_redirect:
                    seg.append(step.text)
                else:
                    seg.append(" " + rm_floor._pull_trailing_fd_prefix(seg) + step.text)
                    in_redirect = True
            elif step.char in "$~" and (
                len(step.text) == 2 or step.state == 1 or (step.char == "~" and step.state)
            ):
                seg.append("\0")
                in_redirect = False
            else:
                seg.append(step.text)
                in_redirect = False
        pos, state = resume, resume_state
    return [*out, "".join(seg)]


def _oracle_carveout(command: str) -> bool:
    if not exfil_bash._exception_eligible(command):
        return False
    words = re.split(r"[ \t]+", command.strip(" \t"))
    return words[0] in exfil_bash._CARVEOUT_VERBS


_WIPE = "rm -" + "rf "  # kept split so this file's text is not itself a wipe command
_HAND_CORPUS = [
    _WIPE + "/",
    _WIPE + "~",
    "echo hi&&" + _WIPE + "~",
    "echo hi&" + _WIPE + "~",
    "a||" + _WIPE + "/",
    "2>/dev/null " + _WIPE + "/",
    "{fd}>x " + _WIPE + "~",
    "/tmp/2>x " + _WIPE + "~",
    _WIPE + "/ 2>&1",
    _WIPE + "~ &>/dev/null",
    _WIPE + "~>|x",
    "echo ';' " + _WIPE + "/",
    "echo '>' x; " + _WIPE + "~",
    'echo "$(' + _WIPE + '~)"',
    'echo "`' + _WIPE + '/`"',
    "cat <<EOF\n" + _WIPE + "/\nEOF",
    "# " + _WIPE + "/\n" + _WIPE + "~",
    "${x:-#} " + _WIPE + "/",
    "$${ " + _WIPE + "~",
    "time -p " + _WIPE + "/",
    "then " + _WIPE + "~",
    "x=1 " + _WIPE + '"$HOME"',
    _WIPE + "\\~",
    _WIPE + "'~'",
    _WIPE + "$'~'",
    "(" + _WIPE + "/)",
    "`" + _WIPE + "~`",
    "${x}" + _WIPE + "~",
    "echo $'a\\'b' ; " + _WIPE + "/",
    "a &&& " + _WIPE + "/",
    "a && && " + _WIPE + "~",
    "bash -c 2> ';' '" + _WIPE + "/'",
    "bash -c 2>/dev/null 'make lint' && git commit -m 'docs: never git push --force to main'",
    "date -" + "d @123",
    "grep -" + "d @x f",
    "'date' -" + "d @1",
    "date\\ x -" + "d @1",
    "  \tdate -" + "d @1",
    "#date -" + "d @1",
    "da\\te -" + "d @1",
    'date"" -' + "d @1",
    "date\u00a0x -" + "d @1",
    "",
    "   ",
    "date",
]
_ALPHABET = [
    *" \t;&|\n`()<>'\"\\$~#{}[]-rmf/2EOF",
    "rm",
    " -rf ",
    "~",
    "/",
    "$(",
    "${",
    "<<",
    "date",
]


def _random_corpus(count: int) -> "list[str]":
    rng = random.Random(19473)  # flake-ok: seeded, so the corpus is the same every run
    return ["".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 18))) for _ in range(count)]


def _outcome(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # the floor fails closed on an error; parity includes the error
        return type(exc)


#: The corpus inputs whose segments differ from the old walk on purpose: a live ``$(…)``,
#: backtick or ``<(…)`` is one word of its command now, where the old walk cut there
#: (pinned in TestSubstitutionWords). The hand inputs are listed by name. The random
#: corpus holds hundreds, so per ``track`` its changed ``(input, new segments)`` rows,
#: in corpus order, are pinned by count and sha256: any other row that changes, or any
#: carved row that stops changing, fails the parity test.
_SEGMENTS_CHANGED_ON_PURPOSE = {
    "hand": frozenset({"`" + _WIPE + "~`"}),
    True: (865, "0b4c6e2435e2b87d45623811e102c07fe420ae718d59f0c2bb878ef8c21b6cd8"),
    False: (863, "45e56fdfa8480857b56d6c5bfb02284172a17b6ee50f22688577bc6a43796f69"),
}


class TestParity:
    @pytest.mark.parametrize("track", [True, False])
    def test_rm_segments_match_the_replaced_walk(self, track):
        changed: list[str] = []
        for command in [*_HAND_CORPUS, *_random_corpus(4000)]:
            if "&>" in command or ">|" in command or "<<" in command:
                # one redirect operator to bash; the old walk stopped at ``<<`` and ``<<<``,
                # where the floor now reads on past a heredoc body (pinned in
                # test_shell_heredoc_reader.py); the verdict parity tests still cover these
                continue
            old = _outcome(_oracle_segments, command, track)
            new = _outcome(rm_floor._segments, command, track)
            # ``&&`` / ``||`` end ONE command now, where the old walk left an empty
            # segment between the two cuts; an empty segment holds no words.
            if isinstance(old, list) and isinstance(new, list):
                old, new = [s for s in old if s], [s for s in new if s]
            if command in _HAND_CORPUS:
                carved = command in _SEGMENTS_CHANGED_ON_PURPOSE["hand"]
                assert (old != new) == carved, repr(command)
            if old != new:
                changed.append(repr((command, new)))
        digest = hashlib.sha256("\n".join(changed).encode()).hexdigest()
        assert (len(changed), digest) == _SEGMENTS_CHANGED_ON_PURPOSE[track]

    def test_rm_targets_match_the_replaced_walk(self, monkeypatch):
        corpus = [*_HAND_CORPUS, *_random_corpus(4000)]
        new = [_outcome(rm_floor._rm_wipe_targets, c) for c in corpus]
        monkeypatch.setattr(rm_floor, "_segments", _oracle_segments)
        old = [_outcome(rm_floor._rm_wipe_targets, c) for c in corpus]
        assert old == new

    def test_deny_verdicts_match_the_replaced_walk(self, monkeypatch):
        corpus = [*_HAND_CORPUS, *_random_corpus(300)]
        new = [is_denied(c) for c in corpus]
        monkeypatch.setattr(rm_floor, "_segments", _oracle_segments)
        old = [is_denied(c) for c in corpus]
        assert old == new

    def test_carveout_matches_the_blank_split_first_word(self):
        for command in [*_HAND_CORPUS, *_random_corpus(4000)]:
            assert exfil_bash._data_at_carveout_allows(command) == _oracle_carveout(command), repr(
                command
            )

    def test_the_wipes_in_the_corpus_stay_refused(self):
        for command in _HAND_CORPUS[:12]:
            assert is_denied(command) is not None, repr(command)
