"""``_SubstitutionDepth``: the case-aware successor to the paren counter.

Every argv window on the floor stops at the first token that ends the argv
while no command substitution is open.  Measured with the bare
``_substitution_depth_delta`` -- a per-token character count with no shell
grammar -- a ``case`` PATTERN's ``)`` (the token ``x)``) scores as a
substitution closer and a pattern's ``|`` (``x|y)``) as a separator, so such a
window closes at the first list operator after ``esac`` and every later clause
is lost.  These tests pin the walker on the token shapes the tokenizer
actually produces (``$(case``, ``x)``, ``(x)``, ``x|y)``, ``:;;``, ``esac;``,
``esac)``), each measured against what bash spans.
"""

from __future__ import annotations

import time

import pytest

from kiro_crew.security.shell_normalizer import (
    _KEEPS_COMMAND_POSITION,
    _QUOTED_BLANK_MARK,
    _QUOTED_TICK_MARK,
    _QUOTED_WORD_MARK,
    _WALKER_GRAMMAR_WORDS,
    _mask_quoted_reserved_words,
    _self_tokens,
    _substitution_depth_delta,
    _SubstitutionDepth,
    normalize_shell_command,
)

_NAME = "kiro" + "crew"


def _window(cmd: str, *, command_position: bool = False, skip: int = 1) -> list[str]:
    """The tokens a consumer window anchored after the first word would scan."""
    depth = _SubstitutionDepth(command_position=command_position)
    scanned: list[str] = []
    for token in normalize_shell_command(cmd)[skip:]:
        scanned.append(token)
        if depth.feed(token):
            break
    return scanned


class TestCasePatternDoesNotCloseTheWindow:
    """A ``kill $( ... )`` whose body ends in a lookup: the lookup stays in the window."""

    @pytest.mark.parametrize(
        "body",
        [
            # the issue's measured spelling
            "case x in x) :;; esac; pgrep -f {n}",
            # a leading ``(`` on the pattern, and the ``$( case`` split spelling
            "case x in (x) :;; esac; pgrep -f {n}",
            " case x in (x) :;; esac; pgrep -f {n}",
            # a pattern alternation: its ``|`` is not a pipe
            "case x in x|y) :;; esac; pgrep -f {n}",
            "case x in (x|y) :;; esac; pgrep -f {n}",
            # several clauses: ``;;`` re-arms the pattern each time
            "case x in x) :;; y) :;; esac; pgrep -f {n}",
            # the other clause terminators
            "case x in x) :;& y) :;;& esac; pgrep -f {n}",
            # an empty case, and a body glued to its pattern
            "case x in esac; pgrep -f {n}",
            "case x in x)pgrep -f {n};; esac",
            # a substitution and an extglob group INSIDE the pattern balance first
            "case x in $(echo x)) :;; esac; pgrep -f {n}",
            "case x in @(a|b)) :;; esac; pgrep -f {n}",
            # nested: a case inside a substitution inside a case body, and a
            # plain nested case whose ``esac;;`` closes the inner and re-arms the outer
            "case x in x) $(case y in y) :;; esac);; esac; pgrep -f {n}",
            "case x in x) case y in y) :;; esac;; esac; pgrep -f {n}",
            # case in every command position bash grants it
            "if true; then case x in x) :;; esac; fi; pgrep -f {n}",
            "while :; do case x in x) :;; esac; break; done; pgrep -f {n}",
            "function f case x in x) :;; esac; pgrep -f {n}",
            "time -p case x in x) :;; esac; pgrep -f {n}",
            "coproc case x in x) :;; esac; pgrep -f {n}",
            # ``esac`` as an ARGUMENT does not disarm; ``esac`` in command position does
            "case x in x) echo esac;; esac; pgrep -f {n}",
            # a clause body glued to its pattern is fed through the grammar too:
            # a nested ``case``, an ``esac``, a substitution opener
            "case x in x)case y in y) :;; esac;; esac; pgrep -f {n}",
            "case x in x)esac; pgrep -f {n}",
            "case x in x)$(case y in y) :;; esac);; esac; pgrep -f {n}",
            # a ``)`` inside a parameter expansion is expansion text, in the
            # pattern and in an ordinary word of the body alike
            "case x in ${{v:-x)}}) :;; esac; pgrep -f {n}",
            "case x in ${{v:-$(echo x)}}) :;; esac; pgrep -f {n}",
            "case x in ${{a:-${{b)}}}}) :;; esac; pgrep -f {n}",
            ": ${{v:-x)}}; pgrep -f {n}",
            # a control operator glued to ``case`` still opens it in command position
            ":;case x in x) :;; esac; pgrep -f {n}",
            "x&&case x in x) :;; esac; pgrep -f {n}",
            "x|case x in x) :;; esac; pgrep -f {n}",
            # the operator alone (``esac;case``): the re-fed ``;case`` re-arms the second case
            "case y in y) :;; esac;case x in x) :;; esac; pgrep -f {n}",
            "case y in y) :;; esac;case x in x) :;; esac;case z in z) :;; esac; pgrep -f {n}",
            "case y in y) :;; esac&&case x in x) :;; esac; pgrep -f {n}",
            # a clause terminator is an operator: the next pattern may be glued to it,
            # and the first pattern may be glued to ``in``
            "case x in x) :;;y) :;; esac; pgrep -f {n}",
            "case x in x) :;&y) :;; esac; pgrep -f {n}",
            "case x in x) :;;&y) :;; esac; pgrep -f {n}",
            "case x in(x) :;; esac; pgrep -f {n}",
            "case x in(x) :;;(y) :;; esac; pgrep -f {n}",
            # a quoted ``esac`` as the pattern: ``esac)`` cannot be followed by ``;;``
            # once the case is closed, so the compound reopens; ``esac|`` is a pattern
            "case x in 'esac') :;; esac; pgrep -f {n}",
            "case x in x) :;; 'esac') :;; esac; pgrep -f {n}",
            "case x in 'esac'|x) :;; esac; pgrep -f {n}",
            # ...and with an EMPTY body the terminator is glued: ``esac);;`` (R13 minted)
            "case x in 'esac');; esac; pgrep -f {n}",
            "case x in 'esac');& esac; pgrep -f {n}",
            "case esac in 'esac');; esac; pgrep -f {n}",
            # a quoted ``)`` in a position bash refuses unquoted is pattern text
            "case x in ')') :;; esac; pgrep -f {n}",
            "case x in 'x)') :;; esac; pgrep -f {n}",
            # the EMPTY pattern de-quotes to a bare ``)``: it terminates
            "case x in '') :;; esac; pgrep -f {n}",
            'case x in "") :;; esac; pgrep -f {n}',
            "case x in '))') :;; esac; pgrep -f {n}",
            "case x in (')') :;; esac; pgrep -f {n}",
            # the WORD spans tokens: ``in`` follows once its substitution closes
            "case $(echo x) in x) :;; esac; pgrep -f {n}",
            "case $(echo x y) in x) :;; esac; pgrep -f {n}",
            "case $(echo $(echo x)) in x) :;; esac; pgrep -f {n}",
        ],
    )
    def test_the_whole_body_is_one_argument(self, body: str) -> None:
        cmd = "kill $(" + body.format(n=_NAME) + ")"
        assert _window(cmd) == normalize_shell_command(cmd)[1:]

    def test_the_counter_alone_still_closes_early(self) -> None:
        # The regression this walker exists for, measured on the raw counter.
        depth = 0
        seen: list[str] = []
        for token in normalize_shell_command("kill $(case x in x) :;; esac; pgrep -f x)")[1:]:
            seen.append(token)
            depth += _substitution_depth_delta(token)
            if depth <= 0 and (";" in token or "|" in token):
                break
        assert seen == ["$(case", "x", "in", "x)", ":;;"]


class TestBoundariesOutsideACaseAreUnchanged:
    """Everything the counter got right, the walker gets right the same way."""

    @pytest.mark.parametrize(
        ("cmd", "expected"),
        [
            ("kill 123; echo $(cat /tmp/x)", ["123;"]),
            ("kill $(true; echo 1)", ["$(true;", "echo", "1)"]),
            ("kill $(pgrep -f other); case x in x) :;; esac; echo x", ["$(pgrep", "-f", "other);"]),
            # a case that STARTS after the window's own argv ended is never entered
            ("kill 123; case x in x) :;; esac; pgrep -f x", ["123;"]),
            # the window's first token is an ARGUMENT: ``case`` there is data
            ("kill case x in x) :;; esac; pgrep -f x", ["case", "x", "in", "x)", ":;;"]),
            # ``case`` after an ordinary verb, an assignment or a redirect is data
            ("kill $(echo case x in x); pgrep -f x)", ["$(echo", "case", "x", "in", "x);"]),
            ("kill $(v=1 case x in x); pgrep -f x)", ["$(v=1", "case", "x", "in", "x);"]),
            (
                "kill $(echo -n case x in x); pgrep -f x)",
                ["$(echo", "-n", "case", "x", "in", "x);"],
            ),
            # a redirect glued to ``esac`` still disarms, and the substitution's
            # own ``)`` behind it closes the window at the separator
            (
                "kill $(case x in a) :;; esac>/dev/null); echo x",
                ["$(case", "x", "in", "a)", ":;;", "esac>/dev/null);"],
            ),
            (
                "kill $(case x in a) :;; esac 2>&1); echo x",
                ["$(case", "x", "in", "a)", ":;;", "esac", "2>&1);"],
            ),
        ],
    )
    def test_window(self, cmd: str, expected: list[str]) -> None:
        assert _window(cmd) == expected

    def test_a_frame_walk_starts_in_command_position(self) -> None:
        # The rsync/ssh outer walk feeds a whole frame from its first token,
        # where ``case`` IS the reserved word: the pattern's ``|`` must not be
        # read as a pipe there either.
        cmd = "case $x in a|b) rsync -e ssh x y;; esac; echo z"
        depth = _SubstitutionDepth(command_position=True)
        boundaries = [tok for tok in normalize_shell_command(cmd) if depth.feed(tok)]
        assert boundaries == ["y;;", "esac;"]

    def test_depth_never_goes_negative(self) -> None:
        depth = _SubstitutionDepth()
        depth.feed(")")
        depth.feed(")")
        assert depth.top_level
        depth.feed("$(x")
        assert not depth.top_level
        depth.feed("y)")
        assert depth.top_level


class TestParensInsideAParameterExpansionAreText:
    """Inside ``${ … }`` a ``)`` is text and a ``$(`` opened there is real; a process
    substitution ``<( … )`` / ``>( … )`` is the command list it opens."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # the case WORD is a process substitution: its ``)`` closes the ``<(``,
            # not the ``$(`` the window is inside of (measured: the counter let
            # ``kirocrew $(case <(printf x) in *) :;; esac) token`` mint)
            "kill $(case <(printf x) in *) :;; esac; pgrep -f x)",
            "kill $(case >(cat) in *) :;; esac; pgrep -f x)",
            # a process substitution in a clause body
            "kill $(case x in x) cat <(echo y);; esac; pgrep -f x)",
        ],
    )
    def test_a_process_substitution_does_not_end_the_window(self, cmd: str) -> None:
        assert _window(cmd) == normalize_shell_command(cmd)[1:]

    def test_a_balanced_process_substitution_still_ends_the_argv(self) -> None:
        cmd = "kill $(pgrep x) <(echo); echo y"
        assert _window(cmd) == ["$(pgrep", "x)", "<(echo);"]

    def test_an_expansion_closer_does_not_end_the_window(self) -> None:
        # ``kill $(: ${v:-x)}; pgrep -f <name>)``: the body is one argument.
        cmd = "kill $(: ${v:-x)}; pgrep -f x)"
        assert _window(cmd) == normalize_shell_command(cmd)[1:]

    def test_a_real_closer_after_an_expansion_still_ends_it(self) -> None:
        cmd = "kill ${PIDS:-$(pgrep x; echo 1)}; echo x"
        assert _window(cmd) == ["${PIDS:-$(pgrep", "x;", "echo", "1)};"]


class TestParensInsideABracketArePatternText:
    """A ``)`` between ``[`` and a later ``]`` was quoted: bash refuses ``[)]`` unquoted."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # measured on R10: ``x[\\)]|x)`` de-quotes to ``x[)]|x)`` and the first
            # ``)`` closed the pattern, so the window ended at the ``)`` after ``]|x``
            "kill $(case x in x[\\)]|x) :;; esac; pgrep -f x)",
            "kill $(case x in x[\\)]) :;; esac; pgrep -f x)",
            "kill $(case x in [\\)]) :;; esac; pgrep -f x)",
            # ``!``/``^`` negation and a leading ``]`` are bracket text
            "kill $(case x in [!\\)]) :;; esac; pgrep -f x)",
            "kill $(case x in [^\\)]) :;; esac; pgrep -f x)",
            "kill $(case x in []\\)]) :;; esac; pgrep -f x)",
            # two quoted parens, a range, and a character class inside the bracket
            "kill $(case x in x[\\)y\\)]) :;; esac; pgrep -f x)",
            "kill $(case x in x[a-z\\)]|y) :;; esac; pgrep -f x)",
            "kill $(case x in [[:alpha:]\\)]) :;; esac; pgrep -f x)",
            # a quoted ``(`` inside the bracket opens no group (bash refuses ``[(]``)
            "kill $(case x in [\\(]) :;; esac; pgrep -f x)",
        ],
    )
    def test_the_bracket_does_not_end_the_window(self, cmd: str) -> None:
        assert _window(cmd) == normalize_shell_command(cmd)[1:]

    @pytest.mark.parametrize(
        ("cmd", "last"),
        [
            # a bracket without a ``)`` inside changes nothing
            ("kill $(case x in [a-z]) :;; esac); echo x", "esac);"),
            ("kill $(case x in [[:alpha:]]) :;; esac); echo x", "esac);"),
            ("kill $(case x in []a]) :;; esac); echo x", "esac);"),
            # an unclosed ``[`` is a literal, so the ``)`` after it still terminates
            ("kill $(case x in x[) :;; esac); echo x", "esac);"),
            # the bracket closes at its ``]`` and the next ``)`` terminates the pattern
            ("kill $(case x in x[\\)]) :;; esac); echo x", "esac);"),
            # a ``]`` inside a nested ``$( )`` does not close the bracket
            ("kill $(case x in [$(echo ])]) :;; esac); echo x", "esac);"),
        ],
    )
    def test_the_pattern_still_terminates(self, cmd: str, last: str) -> None:
        assert _window(cmd)[-1] == last


class TestAnExpansionSplitAcrossTokensIsOneWord:
    """A blank inside ``${ … }`` splits it over tokens; bash reads on to the ``}``.

    A token-local reading forgot the open brace, so ``y)};;`` scored its ``)`` as
    a closer and the window ended at the ``;;`` (measured: ``<name> $(case x in x)
    : ${v:-x y)};; esac; :) <verb>`` minted on the R9 head).  The frame carries
    the open ``${`` across tokens until its ``}``.
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            # in a clause body
            "kill $(case x in x) : ${v:-x y)};; esac; pgrep -f x)",
            # as the case WORD
            "kill $(case ${v:-x y)} in x) :;; esac; pgrep -f x)",
            # outside any case
            "kill $(: ${v:-x y)}; pgrep -f x)",
            # over three tokens, and nested
            "kill $(: ${v:-x y z)}; pgrep -f x)",
            "kill $(: ${v:-${w:-x y)}}; pgrep -f x)",
        ],
    )
    def test_the_body_stays_one_argument(self, cmd: str) -> None:
        assert _window(cmd) == normalize_shell_command(cmd)[1:]

    def test_the_word_ends_where_the_brace_closes(self) -> None:
        cmd = "kill ${v:-x y)}; echo x"
        assert _window(cmd) == ["${v:-x", "y)};"]

    def test_a_carried_word_does_not_arm_a_pattern_early(self) -> None:
        # the ``;;`` inside the expansion is text, the one after it is the clause end
        cmd = "kill $(case x in x) : ${v:-x ;;} ;; esac; pgrep -f x)"
        assert _window(cmd) == normalize_shell_command(cmd)[1:]


class TestAnOpenExpansionIsBounded:
    """A ``${`` that never closes costs each token once and, at top level, nothing."""

    def test_each_token_of_a_split_word_costs_its_own_length(self) -> None:
        # R10 re-reduced the JOINED word on every feed: ~10,000 one-char tokens after an
        # open ``${`` inside a substitution were ~10^8 character steps on the event loop.
        # The frame walk reads every character once (the file's 120 s timeout is the
        # backstop; the pass is well under a second).
        tokens = ["$(:", "${"] + ["a"] * 20000
        depth = _SubstitutionDepth()
        for token in tokens:
            assert depth.feed(token) is False
        assert not depth.top_level
        assert depth.feed("}") is False and depth.feed("x);") is True

    @pytest.mark.parametrize(
        ("cmd", "expected"),
        [
            # a quoted ``'${'`` literal at top level: the argv still ends at the ``;``
            ("kill '${' 1; echo x", ["${", "1;"]),
            ("pkill -f '${a' ; echo x", ["-f", "${a", ";"]),
            # ``$case`` is a parameter, not the reserved word
            ("pkill -f $case ; echo x", ["-f", "$case", ";"]),
        ],
    )
    def test_top_level_shapes_end_the_argv(self, cmd: str, expected: list[str]) -> None:
        assert _window(cmd) == expected

    def test_a_split_expansion_inside_a_substitution_is_still_one_word(self) -> None:
        cmd = "kill $(: ${v:-x y)}; pgrep -f x)"
        assert _window(cmd) == normalize_shell_command(cmd)[1:]

    @pytest.mark.parametrize(
        ("cmd", "expected"),
        [
            # no ``}`` follows anywhere, so bash would refuse an open ``${`` (measured):
            # the quoted literal's ``)`` closes the substitution and the argv ends at ``;``
            ("kill $(pgrep -f 'sync-${ENV'); echo x", ["$(pgrep", "-f", "sync-${ENV);"]),
            ("kill $(pgrep -f 'a${b' 'c)d'); echo x", ["$(pgrep", "-f", "a${b", "c)d);"]),
            # a ``}`` still to come keeps the ``)`` as text: the expansion is real
            ("kill $(: ${v:-x) y}; pgrep -f x)", None),
            ("kill $(: ${v:-x); pgrep -f x}; echo y)", None),
            # a ``}`` with NO ``)`` after it cannot be that closer: the ``$(`` would stay
            # open too (bash refuses it, measured), so the ``${`` was quoted (R12 denied)
            ("kill $(grep -c '${' x); awk '{print}' y; echo x", ["$(grep", "-c", "${", "x);"]),
            # ...and a quoted literal is a WORD, not a function-body opener: R14 rewrote it
            # to ``{`` and the window ended on it (``killall '${' <name>`` was allowed)
            ("kill '${' x; echo y", ["${", "x;"]),
            ("kill '${' >/dev/null x; echo y", ["${", ">/dev/null", "x;"]),
            # an expansion INSIDE an expansion: the outer ``}`` closes only the outer
            # (R14 doubled the inner's open-``$(`` count into the outer and the real
            # ``)`` was read as text, so the argv never ended)
            ("kill ${PIDS:-$(pgrep -f ${SVC})}; echo x", ["${PIDS:-$(pgrep", "-f", "${SVC})};"]),
            ("kill ${a:-${b:-$(c)}}; echo x", ["${a:-${b:-$(c)}};"]),
        ],
    )
    def test_an_unclosable_brace_is_quoted_text(
        self, cmd: str, expected: "list[str] | None"
    ) -> None:
        tokens = normalize_shell_command(cmd)
        depth = _SubstitutionDepth(rest=tokens[1:])
        scanned: list[str] = []
        for token in tokens[1:]:
            scanned.append(token)
            if depth.feed(token):
                break
        assert scanned == (expected if expected is not None else tokens[1:])

    def test_a_backtick_case_opens_the_substitution_it_is_in(self) -> None:
        # ``kirocrew `case x in x) :;; esac; :` token`` runs ``kirocrew token``; the
        # counter scores one backtick as 0, so the window closed at ``:;;`` (R10)
        cmd = "kill `case x in x) :;; esac; :` x"
        assert _window(cmd) == normalize_shell_command(cmd)[1:]


class TestGrammarTokensAreNotOperands:
    """``grammar_next`` is True exactly while the next token is the case WORD, ``in``
    or a PATTERN -- the tokens bash never hands to the command.  The ssh-family walk
    read a ``*)`` pattern as a host that matches every self name (scope rows)."""

    def test_the_word_in_and_patterns_are_grammar(self) -> None:
        depth = _SubstitutionDepth()
        assert depth.feed("$(case") is False and depth.grammar_next
        tokens = ["x", "in", "*)", "echo", "b;;", "y|z)", ":;;", "esac)", "host:/x"]
        grammar = [True, True, True, False, False, True, False, True, False]
        for token, expected in zip(tokens, grammar):
            assert depth.grammar_next is expected, token
            depth.feed(token)
        assert not depth.grammar_next

    def test_outside_a_case_nothing_is_grammar(self) -> None:
        depth = _SubstitutionDepth()
        for token in ["$(echo", "-v)", "localhost", "case", "x)"]:
            assert not depth.grammar_next, token
            depth.feed(token)


class TestGluedClausesAreFedInALoop:
    """A run of glued ``x);;`` clauses is ONE token; R12 recursed twice per clause and a
    2 KB command raised ``RecursionError`` out of the gate instead of a verdict."""

    def test_two_thousand_glued_clauses(self) -> None:
        depth = _SubstitutionDepth()
        for token in ["$(case", "x", "in"]:
            depth.feed(token)
        assert depth.feed("x);;" * 2000 + "esac)") is False
        assert depth.top_level
        assert depth.feed("x;") is True

    def test_a_long_glued_esac_chain(self) -> None:
        depth = _SubstitutionDepth()
        for token in ["$(case", "x", "in"] + ["x)case", "y", "in"] * 600:
            depth.feed(token)
        assert depth.feed("x)" + ":;;esac;" * 601) is False
        assert depth.depth == 1

    def test_an_empty_token_is_still_fed_once(self) -> None:
        """A frame keeps an empty word (``""``) as its own token, and ``_ends_argv("")``
        is a boundary: the R13 loop skipped the token, so the self-protection
        floor read ``<cli> "" restart`` as a restart (CI shard 7)."""
        assert _SubstitutionDepth().feed("") is True
        assert _window('prog "" restart') == [""]
        depth = _SubstitutionDepth()
        for token in ["$(case", "x", "in", '"")', ":;;", "esac"]:
            assert depth.feed(token) is False, token  # an empty pattern is still a pattern
        assert depth.depth == 1 and not depth.top_level


class TestABacktickInsideAPatternIsASubstitution:
    """Between a pattern's backticks a ``)`` is text: a backtick pattern with a quoted
    ``)`` de-quotes to a token whose first ``)`` sits inside the backticks (R12 minted)."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "kill $(case x in `printf ')'`) :;; esac; pgrep -f x)",
            "kill $(case x in `echo x`) :;; esac; pgrep -f x)",
            "kill $(case x in a|`printf ')'`) :;; esac; pgrep -f x)",
            "kill $(case x in `printf ')'`|`printf ')'`) :;; esac; pgrep -f x)",
        ],
    )
    def test_the_body_stays_one_argument(self, cmd: str) -> None:
        assert _window(cmd) == normalize_shell_command(cmd)[1:]

    def test_the_pattern_still_terminates(self) -> None:
        depth = _SubstitutionDepth()
        for token in ["$(case", "x", "in", "`echo", "x`)", ":;;", "esac)"]:
            depth.feed(token)
        assert depth.top_level


class TestASubstitutionInsideAPatternIsItsOwnCommandList:
    """A ``$( … )`` or backtick in a PATTERN is a whole command list: a ``case`` inside
    it has patterns of its own, and their ``)`` close nothing of the enclosing pattern
    or substitution.  R14 read the inner pattern's ``)`` as the substitution's closer
    (GPT: ``<name> $(case x in $(case y in y) :;; esac)) :;; esac; :) <verb>`` minted).
    A quoted ``'esac'`` in body command position, or as a pattern followed by a blank
    and ``)``, seems to close the compound; the ``;;`` that cannot follow a closed
    case re-opens it (deny direction)."""

    @pytest.mark.parametrize(
        "body",
        [
            "case x in $(case y in y) :;; esac)) :;; esac; pgrep -f {n}",
            "case x in $(case y in y) :;; esac; case z in z) :;; esac)) :;; esac; pgrep -f {n}",
            "case x in $(case y in $(case z in z) :;; esac)) :;; esac)) :;; esac; pgrep -f {n}",
            "case x in $(case y in y) :;; esac)|x) :;; esac; pgrep -f {n}",
            "case x in x|$(case y in y) :;; esac)) :;; esac; pgrep -f {n}",
            # the substitution double-quoted: ONE de-quoted token carrying blanks
            'case x in "$(case y in y) :;; esac)") :;; esac; pgrep -f {n}',
            'case x in "$(case y in y) :;; esac)"|x) :;; esac; pgrep -f {n}',
            "case x in `case y in y) :;; esac`) :;; esac; pgrep -f {n}",
            # a nested case in a clause BODY, then a body word that is a quoted esac
            "case x in x) case y in y) :;; esac; 'esac';; z) :;; esac; pgrep -f {n}",
            "case x in x) 'esac';; y) :;; esac; pgrep -f {n}",
            "case x in x) :;; 'esac' ) :;; esac; pgrep -f {n}",
            "case x in $(case y in 'esac') :;; esac)) :;; esac; pgrep -f {n}",
        ],
    )
    def test_the_whole_body_is_one_argument(self, body: str) -> None:
        cmd = "kill $(" + body.format(n=_NAME) + ")"
        assert _window(cmd) == normalize_shell_command(cmd)[1:]

    @pytest.mark.parametrize(
        ("cmd", "last"),
        [
            ("kill $(case x in $(case y in y) :;; esac)) :;; esac); echo x", "esac);"),
            ("kill $(case x in `case y in y) :;; esac`) :;; esac); echo x", "esac);"),
            ("kill $(case x in x) 'esac';; y) :;; esac); echo x", "esac);"),
        ],
    )
    def test_the_substitution_still_closes(self, cmd: str, last: str) -> None:
        assert _window(cmd)[-1] == last


class TestAPrefixedOpenerArmsTheCase:
    """``$x$(case`` opens a compound the way ``$(case`` does -- bash refuses every other
    ``(case`` spelling (``$x(case``, measured), so the prefix never changes the reading."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "kill $x$(case y in y) :;; esac; pgrep -f x)",
            "kill x$(case y in y) :;; esac; pgrep -f x)",
            "kill $x`case y in y) :;; esac; pgrep -f x`",
            "kill $(:)$(case y in y) :;; esac; pgrep -f x)",
        ],
    )
    def test_the_body_stays_one_argument(self, cmd: str) -> None:
        assert _window(cmd) == normalize_shell_command(cmd)[1:]

    def test_a_word_ending_in_case_is_not_the_reserved_word(self) -> None:
        assert _window("pkill -f showcase ; echo x") == ["-f", "showcase", ";"]
        assert _window("kill $(echo showcase); echo x") == ["$(echo", "showcase);"]


class TestNewlinesInsideTheGrammarAreTransparent:
    """A frame renders a newline as a standalone ``;``; the case state holds across it."""

    @pytest.mark.parametrize(
        "frame",
        [
            # newline between the WORD and ``in``, one and two of them
            ["$(case", "x", ";", "in", "x)", ":;;", "esac;", "pgrep", "-f", "x)"],
            ["$(case", "x", ";", ";", "in", "x)", ":;;", "esac;", "pgrep", "-f", "x)"],
            # newline between ``in`` and the first pattern, and between ``;;`` and the next
            ["$(case", "x", "in", ";", "x)", ":;;", "esac;", "pgrep", "-f", "x)"],
            ["$(case", "x", "in", "x)", ":;;", ";", "y)", ":;;", "esac;", "pgrep", "-f", "x)"],
            # newline between ``;;`` and ``esac``: ``esac`` still reads as the word
            ["$(case", "x", "in", "x)", ":;;", ";", "esac;", "pgrep", "-f", "x)"],
        ],
    )
    def test_the_body_stays_one_argument(self, frame: list[str]) -> None:
        depth = _SubstitutionDepth()
        ended = [depth.feed(tok) for tok in frame]
        assert ended == [False] * len(frame)
        assert depth.top_level

    def test_esac_after_a_newline_closes_the_case_for_real(self) -> None:
        # ``kill $(case x in x) :;;<newline>esac); echo <name>``: the ``esac)``
        # must close the compound AND the substitution, so the window ends at ``;``.
        depth = _SubstitutionDepth()
        frame = ["$(case", "x", "in", "x)", ":;;", ";", "esac);", "echo", "x"]
        ended = [depth.feed(tok) for tok in frame]
        assert ended == [False, False, False, False, False, False, True, False, False]
        assert depth.top_level


class TestDataTokensStillAdvanceAPendingPattern:
    """``feed_data``: a caller's data token is inert unless bash reads it as the pattern."""

    def test_outside_a_case_nothing_changes(self) -> None:
        depth = _SubstitutionDepth()
        depth.feed("$(x")
        assert depth.feed_data("print(1); y)") is False
        assert depth.depth == 1  # the payload's parens and separator are data

    def test_an_option_shaped_pattern_is_still_the_pattern(self) -> None:
        # ``case $1 in -c) echo hi;; esac); grep x`` -- an operand scan reads
        # ``-c)`` as an option and would skip it; the pattern must close anyway.
        depth = _SubstitutionDepth()
        for tok in ["$(case", "$1", "in"]:
            depth.feed(tok)
        assert depth.feed_data("-c)") is False
        ended = [depth.feed(tok) for tok in ["echo", "hi;;", "esac);"]]
        assert ended == [False, False, True]
        assert depth.top_level


class TestDataTokensOpenButDoNotCloseAnEarlierFrame:
    """``feed_data``: a substitution the data token opens is read through; a ``)`` in
    it closes only a frame the same token opened (a quoted one in a payload is text)."""

    def test_a_redirect_target_opens_a_substitution(self) -> None:
        depth = _SubstitutionDepth()
        assert depth.feed_data("2>$(case") is False
        assert depth.depth == 1
        for token in ["x", "in", "x)", ":;;"]:
            depth.feed(token)
        assert depth.feed("esac;") is False  # inside the substitution, not the argv's end
        assert [depth.feed(tok) for tok in ["echo", "/dev/null)", "x"]] == [False, False, False]
        assert depth.top_level and depth.words == ["x"]

    def test_a_complete_substitution_in_a_data_token_nets_zero(self) -> None:
        depth = _SubstitutionDepth()
        assert depth.feed_data("2>$(mktemp)") is False
        assert depth.top_level and depth.words == []
        assert depth.feed(";") is True

    def test_a_quoted_operator_in_a_data_token_is_text(self) -> None:
        depth = _SubstitutionDepth()
        for token in ["a;b", "c|d", "e(f)", "#g"]:
            assert depth.feed_data(token) is False
        assert depth.top_level and depth.words == []


class TestTopLevelWordsAreListed:
    """``words``: the argument words the token completed, substitutions cut out."""

    def test_a_suffix_glued_to_a_closer_is_the_word(self) -> None:
        depth = _SubstitutionDepth()
        for token in ["$(case", "x", "in", "x)", ":;;"]:
            depth.feed(token)
        assert depth.feed("esac)verb") is False
        assert depth.words == ["verb"]

    @pytest.mark.parametrize("tokens", [["$(echo", "x)"], ["`echo", "x`"]])
    def test_a_word_inside_a_substitution_is_not_listed(self, tokens: list[str]) -> None:
        depth = _SubstitutionDepth()
        assert [depth.words for tok in tokens if depth.feed(tok) is False] == [[], []]

    def test_a_plain_word_and_a_split_word(self) -> None:
        depth = _SubstitutionDepth()
        depth.feed("status")
        assert depth.words == ["status"]
        depth.feed("re$(:)start")
        assert depth.words == ["re" + "start"]  # the text bash glues around the output

    def test_the_list_is_per_token(self) -> None:
        depth = _SubstitutionDepth()
        depth.feed("a")
        depth.feed("$(b")
        assert depth.words == []


class TestAKeeperHandsOnOnlyAPositionItHolds:
    """``time``/``if``/an option word keep command position; they do not create it."""

    def test_in_argument_position_time_is_a_word(self) -> None:
        depth = _SubstitutionDepth()
        for token in ["/etc/passwd", "time", "case", "x", "in"]:
            depth.feed(token)
        assert depth.grammar_next is False
        assert depth.words == ["in"]

    def test_in_command_position_time_arms_the_case(self) -> None:
        depth = _SubstitutionDepth(command_position=True)
        for token in ["time", "-p", "case", "x", "in"]:
            depth.feed(token)
        assert depth.grammar_next is True

    def test_function_takes_a_name_only_in_command_position(self) -> None:
        depth = _SubstitutionDepth()
        for token in ["arg", "function", "case", "x", "in"]:
            depth.feed(token)
        assert depth.grammar_next is False


class TestEsacReArms:
    """After ``esac`` the walker is out of the case: a later ``)`` closes for real."""

    def test_a_closer_after_esac_counts(self) -> None:
        depth = _SubstitutionDepth()
        for token in normalize_shell_command("kill $(case x in x) :;; esac)")[1:]:
            ended = depth.feed(token)
        assert depth.top_level
        assert ended is False

    def test_a_pattern_after_esac_is_a_closer_again(self) -> None:
        # Once the compound is closed, ``x)`` is what it always was to the counter.
        depth = _SubstitutionDepth()
        for token in ["$(case", "x", "in", "x)", ":;;", "esac;", "$(echo", "x)"]:
            depth.feed(token)
        assert depth.depth == 1
        depth.feed("x)")
        assert depth.top_level


class TestAQuotedEsacPatternIsSettledByLookahead:
    """``esac)`` in PATTERN position reads two ways -- the reserved word glued to the
    substitution's closer, or a quoted ``'esac')`` pattern whose clause body follows.
    R16 (GPT) always read the closer: the body's ``;`` ended the window and the verb
    behind the REAL closer was never read.  The rest of the argv settles it: under
    the closer reading the tokens after it are top-level text, where bash refuses a
    ``)`` closing nothing, a ``;;`` with no case open and ``esac`` in command position.
    """

    @staticmethod
    def _scan(tokens: list[str]) -> tuple[list[bool], _SubstitutionDepth]:
        depth = _SubstitutionDepth(rest=tokens)
        return [depth.feed(token) for token in tokens], depth

    def test_a_body_then_a_clause_terminator_proves_the_pattern(self) -> None:
        tokens = ["$(case", "x", "in", "esac)", "echo", "hi;", ":;;", "esac;", ":)", "tok"]
        ended, depth = self._scan(tokens)
        assert ended == [False] * len(tokens)  # the body's ``;`` did not end the window
        assert depth.words == ["tok"]  # the verb behind the real closer is read

    def test_a_body_then_the_compound_s_esac_proves_the_pattern(self) -> None:
        tokens = ["$(case", "x", "in", "esac)", "echo", "hi;", "esac;", ":)", "tok"]
        ended, depth = self._scan(tokens)
        assert ended == [False] * len(tokens)
        assert depth.top_level and depth.words == ["tok"]

    def test_a_body_then_the_real_closer_proves_the_pattern(self) -> None:
        # ``$(case x in 'esac') echo hi)`` is not valid bash either way (the compound
        # is left open), and the closer that nothing else explains is the second reading.
        tokens = ["$(case", "x", "in", "esac)", "echo", "hi;", "x)", "tok"]
        ended, depth = self._scan(tokens)
        assert ended == [False] * len(tokens)
        assert depth.top_level and depth.words == ["tok"]

    @pytest.mark.parametrize(
        "after",
        [
            ["status;", "echo", "tok"],  # a word, then the window ends at the ``;``
            [";", "echo", "tok"],  # a standalone ``;`` (a newline sentinel), then a command
            ["|", "grep", "tok"],
            ["nginx;", "echo", "tok", "$(x)"],  # a later substitution closes ITSELF
        ],
    )
    def test_a_rest_bash_accepts_under_the_closer_reading_is_the_closer(
        self, after: list[str]
    ) -> None:
        tokens = ["$(case", "x", "in", "x)", ":;;", "esac)", *after]
        ended, depth = self._scan(tokens)
        assert depth.top_level
        first_end = ended.index(True)
        assert tokens[first_end] in ("status;", ";", "|", "nginx;")

    @pytest.mark.parametrize("glued", ["esac);", "esac)|", "esac)&", "esac))"])
    def test_a_lone_separator_glued_behind_the_closer_is_the_closer(self, glued: str) -> None:
        # Bash refuses an empty clause body before ``;``, ``|`` or ``&`` (``x) ;``), so
        # the glued spelling needs no lookahead -- even when a ``;;`` follows later.
        tokens = ["$(case", "x", "in", "x)", ":;;", glued, ":;;", "echo", "tok"]
        depth = _SubstitutionDepth(rest=tokens)
        for token in tokens[:6]:
            depth.feed(token)
        assert depth.top_level

    def test_a_newline_after_the_pattern_is_allowed(self) -> None:
        # The tokenizer renders a newline as a standalone ``;`` token, and bash allows
        # one right after a pattern's ``)``: ``'esac')<newline> echo hi;; esac; :)``.
        tokens = ["$(case", "x", "in", "esac)", ";", "echo", "hi;;", "esac;", ":)", "tok"]
        ended, depth = self._scan(tokens)
        assert ended == [False] * len(tokens)
        assert depth.words == ["tok"]

    def test_without_the_rest_the_closer_reading_stands(self) -> None:
        depth = _SubstitutionDepth()
        for token in ["$(case", "x", "in", "esac)", "echo"]:
            depth.feed(token)
        assert depth.top_level
        assert depth.feed("hi;") is True

    def test_one_lookahead_covers_a_run_of_ambiguities(self) -> None:
        # Linear: the lookahead from the first ``esac)`` reaches the end and settles
        # every later one; a run of 2000 needs no second pass.
        unit = ["$(case", "x", "in", "x)", ":;;", "esac)"]
        tokens = unit * 2000 + ["a;", "tok"]
        depth = _SubstitutionDepth(rest=tokens)
        for token in tokens[:-1]:
            ended = depth.feed(token)
        assert ended is True and depth.top_level
        unit = ["$(case", "x", "in", "esac)", "echo", "hi;;", "esac;", ":)"]
        tokens = unit * 2000 + ["tok"]
        ended, depth = self._scan(tokens)
        assert True not in ended and depth.words == ["tok"]


class TestACommentStartsTheToken:
    """A ``#`` ends the argv only when it STARTS the token (``_ends_argv``): a blank
    that survived tokenization was quoted, so the ``#`` behind it is data.  R16 read
    ``>'a #'`` as a comment and the verb behind it minted (Opus)."""

    def test_a_hash_behind_a_quoted_blank_is_data(self) -> None:
        depth = _SubstitutionDepth()
        assert depth.feed(">a #") is False
        assert depth.feed("a #") is False
        assert depth.feed("tok") is False and depth.words == ["tok"]

    def test_a_hash_that_starts_the_token_is_a_comment(self) -> None:
        depth = _SubstitutionDepth()
        assert depth.feed("#") is True
        assert _SubstitutionDepth().feed("#a") is True


class TestAQuotedGrammarWordIsAnArgument:
    """bash reads ``case``/``esac``/``in`` and the keepers as grammar only when no
    character of the word is quoted; the tokenizer drops the quotes, so
    :func:`_self_tokens` glues :data:`_QUOTED_WORD_MARK` onto a quoted spelling from
    the raw text and the walker reads an ordinary word.  R17 (GPT): ``$("case" x in
    y) host`` armed a pattern, its ``)`` ended the pattern, and the host behind the
    real closer was never this command's operand."""

    @pytest.mark.parametrize(
        "raw",
        [
            '"case"',
            "'case'",
            "\\case",
            "ca\\se",
            "$'case'",
            '$"case"',
            'ca"se"',
            "c'a'se",
            '"esac"',
            "'in'",
            '"time"',
            "'if'",
            '"!"',
            "'{'",
            '"function"',
            "$'\\x63ase'",
            "$'\\143ase'",
            "$'e\\x73ac'",
        ],
    )
    def test_a_quoted_spelling_carries_the_mark(self, raw: str) -> None:
        masked = _mask_quoted_reserved_words(f"x {raw} y")
        if raw.startswith("$'"):
            # an ANSI-C body is emitted DECODED (re-escaped), then marked
            assert masked.endswith(f"'{_QUOTED_WORD_MARK} y")
        else:
            assert masked == f"x {raw}{_QUOTED_WORD_MARK} y"
        tokens = _self_tokens(f"x {raw} y")
        assert tokens[1].endswith(_QUOTED_WORD_MARK)
        assert tokens[1] not in _WALKER_GRAMMAR_WORDS

    @pytest.mark.parametrize(
        "raw",
        [
            "x case y",
            "x esac y",
            "case x in y) :;; esac",
            "x case y z",  # unquoted: nothing to mark
            "x 'localhost' y",  # a quoted ordinary word
            'x "cas"ey z',  # de-quotes to ``casey``
            "x c\\\nase y",  # a line continuation is not quoting
            "echo case; ssh host",
        ],
    )
    def test_everything_else_is_left_verbatim(self, raw: str) -> None:
        assert _mask_quoted_reserved_words(raw) == raw
        assert _QUOTED_WORD_MARK not in "".join(_self_tokens(raw))

    def test_an_escaped_quote_inside_double_quotes_does_not_close_them(self) -> None:
        raw = 'x "ca\\"se" y'
        assert _mask_quoted_reserved_words(raw) == raw  # de-quotes to ``ca"se``
        raw = "x 'ca\\'se y"  # the backslash is text; ``'ca\\'`` then ``se`` glue
        assert _mask_quoted_reserved_words(raw) == raw

    def test_the_marked_word_does_not_arm_a_case(self) -> None:
        depth = _SubstitutionDepth()
        for token in _self_tokens('ssh $("case" x in y 2>/dev/null) localhost')[1:]:
            ended = depth.feed(token)
        assert ended is False
        assert depth.top_level
        assert depth.words == ["localhost"]

    def test_the_unquoted_spelling_still_arms_it(self) -> None:
        depth = _SubstitutionDepth()
        for token in _self_tokens("ssh $(case x in y) localhost")[1:]:
            depth.feed(token)
        assert not depth.top_level
        assert depth.words == []

    def test_a_marked_grammar_word_is_an_operand_without_its_mark(self) -> None:
        from kiro_crew.security.shell_normalizer import _unmark

        depth = _SubstitutionDepth()
        words: list[str] = []
        for token in _self_tokens('ssh remote.example.com "case" "in"')[1:]:
            depth.feed(token)
            words += depth.words  # per feed: the words this token completed
        assert [_unmark(w) for w in words] == ["remote.example.com", "case", "in"]

    def test_a_quoted_in_ends_the_case_word_reading(self) -> None:
        depth = _SubstitutionDepth()
        for token in _self_tokens('ssh $(case x "in" y) localhost')[1:]:
            depth.feed(token)
        assert depth.top_level and depth.words == ["localhost"]

    def test_the_vocabulary_is_the_walker_s_and_bash_s(self) -> None:
        from kiro_crew.security.argv_floor import _SHELL_RESERVED_WORDS

        assert {"case", "esac", "in", "function"} <= _WALKER_GRAMMAR_WORDS
        assert _KEEPS_COMMAND_POSITION <= _WALKER_GRAMMAR_WORDS
        assert _WALKER_GRAMMAR_WORDS <= _SHELL_RESERVED_WORDS
        assert _QUOTED_WORD_MARK not in "".join(_WALKER_GRAMMAR_WORDS)


class TestAQuotedBlankAtTopLevelIsText:
    """A blank inside a token was quoted.  At top level it is text of the ONE word
    bash hands over (``'psql -h localhost'`` is one ssh argument, R17); inside a
    substitution it is that command list's own separator (``"$( … )"`` is one token)."""

    def test_the_word_is_whole(self) -> None:
        depth = _SubstitutionDepth()
        assert depth.feed("psql -h localhost") is False
        assert depth.words == ["psql -h localhost"]
        assert depth.feed("$(true)'a b'".replace("'", "")) is False
        assert depth.words == ["a b"]

    def test_inside_a_substitution_it_still_separates(self) -> None:
        depth = _SubstitutionDepth()
        assert depth.feed("$(case y in y) :;; esac; echo hi)tok") is False
        assert depth.top_level and depth.words == ["tok"]
        depth = _SubstitutionDepth()
        depth.feed("$(a b")
        assert not depth.top_level
        assert depth.feed("c)") is False and depth.top_level


class TestAQuotedBacktickIsText:
    """bash reads a backtick inside quotes or behind a backslash as text; the tokenizer
    drops the quote, so :func:`_self_tokens` rewrites it to :data:`_QUOTED_TICK_MARK`
    and the walker opens no substitution on it (R18 Opus: the frame stayed open and
    the ssh destination behind ``-l 'a`b'`` was never checked)."""

    @pytest.mark.parametrize(
        "raw", ["'a`b'", "a\\`b", "$'a`b'", "'`'", "'a`' 'b`'", '"a\\`b"', "$'\\x60'", "$'a\\140b'"]
    )
    def test_the_mark_replaces_it(self, raw: str) -> None:
        tokens = _self_tokens(f"x {raw} y")
        assert "`" not in "".join(tokens)
        assert _QUOTED_TICK_MARK in "".join(tokens)

    def test_an_ansi_c_body_still_decodes_the_same(self) -> None:
        # The body is emitted decoded and re-escaped for the tokenizer, so every other
        # escape reads exactly as before the mask ran.
        from kiro_crew.security.shell_normalizer import _unmark

        assert [_unmark(t) for t in _self_tokens("echo $'a\\'b\\nc' x")] == ["echo", "a'b c", "x"]
        assert _self_tokens("echo $'a\\\\b' x") == ["echo", "a\\b", "x"]

    def test_an_unquoted_backtick_is_left_alone(self) -> None:
        assert _self_tokens("x `echo a` y") == ["x", "`echo", "a`", "y"]
        assert _QUOTED_TICK_MARK not in "".join(_self_tokens("x `echo a` y"))
        # inside DOUBLE quotes bash still substitutes, so the backtick stays one
        assert _QUOTED_TICK_MARK not in "".join(_self_tokens('x "`echo a`" y'))

    def test_the_walker_stays_at_top_level_and_restores_the_text(self) -> None:
        depth = _SubstitutionDepth()
        for token in _self_tokens("ssh -l 'a`b' localhost")[1:]:
            assert depth.feed(token) is False
            assert depth.top_level
        assert depth.words == ["localhost"]
        depth = _SubstitutionDepth()
        depth.feed(_self_tokens("x 'a`b'")[1])
        assert depth.words == [f"a{_QUOTED_TICK_MARK}b"]  # the mark stays for the consumer

    def test_the_marks_do_not_collide_with_the_separator_sentinels(self) -> None:
        from kiro_crew.security.argv_floor import _QUOTED_SEP_SENTINELS, _unmask_separators
        from kiro_crew.security.shell_normalizer import _SUBST_MARK

        taken = set(_QUOTED_SEP_SENTINELS.values())
        assert not taken & {_SUBST_MARK, _QUOTED_WORD_MARK, _QUOTED_TICK_MARK}
        assert _unmask_separators("a\x00b" + _QUOTED_WORD_MARK + _QUOTED_TICK_MARK) == "a;b`"


class TestAGluedRunOfAmbiguitiesCostsOnePass:
    """R19 Opus: ``"esac);;"`` x 2,900 in ONE token asked the lookahead once per
    clause, and each speculation read the rest of the token -- quadratic.  Pinned
    structurally: the speculations together read a bounded multiple of the token, and
    the cached answer is reused for a later ambiguity before the event it found."""

    def test_speculations_read_a_bounded_multiple_of_the_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        specs: list[_SubstitutionDepth] = []
        real = _SubstitutionDepth._speculation

        def recording(self: _SubstitutionDepth) -> _SubstitutionDepth:
            spec = real(self)
            specs.append(spec)
            return spec

        monkeypatch.setattr(_SubstitutionDepth, "_speculation", recording)
        token = "$(case x in " + "esac);;" * 500
        depth = _SubstitutionDepth(rest=[token, "tok"])
        assert depth.feed(token) is False
        assert specs, "the ambiguity was never asked"
        assert sum(spec._read for spec in specs) < 4 * len(token)
        assert not depth.top_level  # every ``esac)`` read as the quoted pattern

    def test_the_answer_is_reused_before_the_event_it_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        specs: list[_SubstitutionDepth] = []
        real = _SubstitutionDepth._speculation

        def recording(self: _SubstitutionDepth) -> _SubstitutionDepth:
            spec = real(self)
            specs.append(spec)
            return spec

        monkeypatch.setattr(_SubstitutionDepth, "_speculation", recording)
        # Two ambiguities; the first lookahead's event (``esac`` in command position
        # under the closer reading) sits AT the second, so the second reuses it.
        token = "$(case x in esac) esac) tok"
        depth = _SubstitutionDepth(rest=[token, "tok"])
        assert depth.feed(token) is False
        assert len(specs) == 1
        assert depth._lookahead is not None and depth._lookahead[:2] == (0, 0)
        assert depth.top_level  # ``'esac')`` pattern, then the real ``esac`` and closer

    def test_a_long_token_is_normalized_but_not_retained(self) -> None:
        from kiro_crew.security.shell_normalizer import (
            _OPERAND_CACHE_MAX_LEN,
            _normalize_operand,
            _normalize_operand_cached,
        )

        assert _OPERAND_CACHE_MAX_LEN == 128  # the bound is pinned, not read back
        before = _normalize_operand_cached.cache_info()
        assert _normalize_operand("y" * 129) == "y" * 129  # over the bound: not retained
        after = _normalize_operand_cached.cache_info()
        assert after.currsize == before.currsize and after.misses == before.misses
        assert _normalize_operand("z" * 128) == "z" * 128  # at the bound: cached
        assert _normalize_operand_cached.cache_info().misses == before.misses + 1


class TestAnUnterminatedOpenerRunIsProbedOnce:
    """R27 Opus: ``echo "`` + ``$(`` x 10,000 (20 KB, under the ceiling) probed every
    opener for a closer it could not have, each probe reading to the end of the text --
    quadratic, minutes per descent.  The first opener with no closer ahead is one bash
    refuses the whole line for, so every later opener is text without a probe."""

    @staticmethod
    def _probes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
        from kiro_crew.security import quoted_marks

        starts: list[int] = []
        real = quoted_marks._closer_probe

        def recording(
            text: str, start: int, closer: str, *, case_aware: bool
        ) -> "tuple[int, bool]":
            starts.append(start)
            return real(text, start, closer, case_aware=case_aware)

        monkeypatch.setattr(quoted_marks, "_closer_probe", recording)
        return starts

    def test_a_run_of_openers_inside_one_quote(self, monkeypatch: pytest.MonkeyPatch) -> None:
        starts = self._probes(monkeypatch)
        text = 'echo "' + "$(" * 10_000
        started = time.perf_counter()
        masked = _mask_quoted_reserved_words(text)
        assert time.perf_counter() - started < 1.0
        assert starts == [8]  # the first opener only
        assert masked.translate(str.maketrans("", "", _QUOTED_BLANK_MARK)) == text

    def test_a_run_of_quoted_regions_each_with_an_opener(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        starts = self._probes(monkeypatch)
        text = "echo " + '"$(" ' * 4_000
        started = time.perf_counter()
        _mask_quoted_reserved_words(text)
        assert time.perf_counter() - started < 1.0
        assert len(starts) == 1

    def test_a_run_of_open_cases_is_read_case_aware_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``$(case x)`` x 2,200: the case-aware reading never finds a closer (the case
        # is never closed) and the plain one does, so ``refused`` never latched and
        # every opener re-read the text case-aware (R30 Opus).  Once one case-aware
        # probe has read to the end, every later opener is read plain.
        from kiro_crew.security import quoted_marks

        modes: list[bool] = []
        real = quoted_marks._closer_probe

        def recording(
            text: str, start: int, closer: str, *, case_aware: bool
        ) -> "tuple[int, bool]":
            modes.append(case_aware)
            return real(text, start, closer, case_aware=case_aware)

        monkeypatch.setattr(quoted_marks, "_closer_probe", recording)
        text = 'ssh "' + "$(case x)" * 2_200 + 'localhost"'
        started = time.perf_counter()
        _mask_quoted_reserved_words(text)
        assert time.perf_counter() - started < 1.0
        assert modes.count(True) == 1 and len(modes) == 2_200

    def test_the_rest_of_the_line_reads_as_it_did(self) -> None:
        # bash refuses ``x "$(a" "$(b)" y`` at the first opener; the later one is text
        # like it, so the enclosing quotes still close where they do
        assert _self_tokens('x "$(a" "$(b)" y') == ["x", "$(a", "$(b)", "y"]
        assert _self_tokens('x "$(a" y; ' + _NAME + " token") == ["x", "$(a", "y;", _NAME, "token"]


class TestACommentInsideASubstitutionRunsToTheNewline:
    """R35 GPT: a ``#`` word inside a substitution is a comment to the newline, which
    the tokenizer hands the walker as a standalone ``;``; the ``)`` inside it closes
    nothing, and the frame closes at the ``)`` bash reads."""

    def test_the_commented_closer_closes_nothing(self) -> None:
        rest = ["$(:", "#", ")", ";", "echo)", "tok"]
        depth = _SubstitutionDepth(rest=rest)
        seen = []
        for token in rest:
            depth.feed(token)
            seen.append((depth.top_level, list(depth.words)))
        assert seen[2][0] is False  # the commented ``)`` did not close the frame
        assert seen[4][0] is True  # ``echo)`` did
        assert seen[5] == (True, ["tok"])

    def test_a_hash_inside_a_word_or_a_data_token_is_text(self) -> None:
        depth = _SubstitutionDepth(rest=["$(echo", "a#b)", "tok"])
        for token in ["$(echo", "a#b)"]:
            depth.feed(token)
        assert depth.top_level
        depth = _SubstitutionDepth(rest=["$(:", "2>", "a #)", ")", "tok"])
        depth.feed("$(:")
        depth.feed_data("2>")
        depth.feed_data("a #)")  # a quoted target: its ``#`` opens no comment
        assert not depth._comment
        depth.feed(")")
        assert depth.top_level

    def test_a_top_level_comment_still_ends_the_argv(self) -> None:
        depth = _SubstitutionDepth()
        assert depth.feed("#") is True

    def test_the_raw_pass_drops_the_comment_before_tokenizing(self) -> None:
        # a ``;`` inside the comment would be indistinguishable from the newline
        # sentinel once tokenized, so the comment is dropped from the raw text (R37 GPT)
        assert _self_tokens("x $(echo # ; )\n :) y") == ["x", "$(echo", ";", ":)", "y"]
        assert _self_tokens("x $(: # )\n echo) y") == ["x", "$(:", ";", "echo)", "y"]
        assert _self_tokens("x $(:)# y") == ["x", "$(:)#", "y"]  # not a word start
        assert _self_tokens("x $(echo a#b) y") == ["x", "$(echo", "a#b)", "y"]
        assert _self_tokens("x 'a # b' y") == [
            "x",
            f"a{_QUOTED_BLANK_MARK}#{_QUOTED_BLANK_MARK}b",
            "y",
        ]
        assert _self_tokens("x y # z") == ["x", "y"]
        # only an OPENING backtick starts a word: ``\`true\`#x`` is one word (R38 GPT)
        assert _self_tokens("echo `true`#x; y z") == ["echo", "`true`#x;", "y", "z"]
        assert _self_tokens("x `echo #c\n` y") == ["x", "`echo", ";", "`", "y"]


class TestAQuotedBraceOpenerDoesNotSwallowTheCloser:
    """R31 Opus: one brace-closure answer latched from the whole argv armed a ``${``
    group for a QUOTED ``'${'`` when a ``}`` and a later ``)`` stood anywhere ahead, and
    the substitution's own ``)`` was then read as brace text -- the frame never closed
    and the destination behind it was never a top-level word.  Bash refuses ``$(x ${ )``,
    so a ``)`` met with a ``${`` still open in a substitution is its closer."""

    def test_the_closer_closes_over_an_open_brace(self) -> None:
        rest = ["$(x", "${", ")", "localhost", "}", "$(:)"]
        depth = _SubstitutionDepth(rest=rest)
        seen: list[tuple[bool, list[str]]] = []
        for token in rest:
            depth.feed(token)
            seen.append((depth.top_level, list(depth.words)))
        assert seen[2][0] is True  # ``)`` closed the substitution
        assert seen[3] == (True, ["localhost"])

    def test_a_real_brace_expansion_still_spans_its_substitution(self) -> None:
        rest = ["$(echo", "${x:-$(y)})", "localhost"]
        depth = _SubstitutionDepth(rest=rest)
        depth.feed(rest[0])
        assert depth.feed(rest[1]) is False and depth.top_level
        depth.feed(rest[2])
        assert depth.words == ["localhost"]


class TestAQuotedBlankInsideAWordIsMarked:
    """``case "x y" in "x y")`` names the WORD ``x y``; the tokenizer dropped the quote
    and the walker split the blank (R22 GPT).  :func:`_self_tokens` marks a quoted
    blank as text -- unless the quote encloses a substitution, whose blanks are that
    command list's separators (``"$(case y in y) :;; esac)"`` is one token, R11)."""

    def test_a_quoted_blank_is_one_word_s_text(self) -> None:
        assert _self_tokens('x "a b" y') == ["x", f"a{_QUOTED_BLANK_MARK}b", "y"]
        assert _self_tokens("x 'a b' y") == ["x", f"a{_QUOTED_BLANK_MARK}b", "y"]
        assert _self_tokens("x $'a b' y") == ["x", f"a{_QUOTED_BLANK_MARK}b", "y"]
        assert _self_tokens("x ca\\ se y") == ["x", f"ca{_QUOTED_BLANK_MARK}se", "y"]
        from kiro_crew.security.shell_normalizer import _QUOTED_NL_MARK, _QUOTED_TAB_MARK, _unmark

        assert _self_tokens("x 'a\tb' y") == ["x", f"a{_QUOTED_TAB_MARK}b", "y"]
        assert _self_tokens('x "a\nb" y') == ["x", f"a{_QUOTED_NL_MARK}b", "y"]
        assert _unmark(f"a{_QUOTED_TAB_MARK}b{_QUOTED_NL_MARK}c") == "a\tb\nc"

    def test_quoting_restarts_inside_an_enclosed_substitution(self) -> None:
        # bash reads the inner quotes as quotes (R23): ``'case'`` is marked, the body's
        # own blanks still separate its words, and the closer restores the outer quote
        tokens = _self_tokens("x \"$('case' a b) c d\" y")
        assert tokens == [
            "x",
            f"$('case'{_QUOTED_WORD_MARK} a b){_QUOTED_BLANK_MARK}c{_QUOTED_BLANK_MARK}d",
            "y",
        ]

    def test_a_nested_quote_does_not_end_the_enclosing_one(self) -> None:
        # ``"$(dirname "$0")/a"`` is ONE word: the inner quotes belong to the restarted
        # substitution, whose closer a quote-aware scan finds (R25 Opus)
        assert _self_tokens('scp "$(dirname "$0")/a" b h:/x') == [
            "scp",
            "$(dirname $0)/a",
            "b",
            "h:/x",
        ]
        assert _self_tokens('x "$(echo ")") e" y') == ["x", f"$(echo )){_QUOTED_BLANK_MARK}e", "y"]
        # an unterminated opener is text, so the enclosing quote still closes
        assert _self_tokens('x "$(a" y z') == ["x", "$(a", "y", "z"]
        # a ``case`` in COMMAND position defers the closer past the pattern's ``)``;
        # one in argument position does not (R26 GPT)
        assert _self_tokens('x "$(f; case a in a) :;; esac) g" y') == [
            "x",
            f"$(f; case a in a) :;; esac){_QUOTED_BLANK_MARK}g",
            "y",
        ]
        assert _self_tokens('x "$(echo case) e" y') == [
            "x",
            f"$(echo case){_QUOTED_BLANK_MARK}e",
            "y",
        ]
        # ...and a keeper or an option word HOLDS command position, as in the walker:
        # ``time -p case`` opens the case whose pattern ``)`` is not the closer, while
        # ``echo time case`` does not (R28 GPT)
        assert _self_tokens('x "$(time -p case a in a) :;; esac) g" y') == [
            "x",
            f"$(time -p case a in a) :;; esac){_QUOTED_BLANK_MARK}g",
            "y",
        ]
        assert _self_tokens('x "$(if case a in a) :;; esac; then :; fi) g" y') == [
            "x",
            f"$(if case a in a) :;; esac; then :; fi){_QUOTED_BLANK_MARK}g",
            "y",
        ]
        assert _self_tokens('x "$(echo time case a in a) :;; esac) g" y') == [
            "x",
            f"$(echo time case a in a){_QUOTED_BLANK_MARK}:;;{_QUOTED_BLANK_MARK}esac){_QUOTED_BLANK_MARK}g",
            "y",
        ]
        # ...and the NAME after ``function``/``coproc`` hands command position on, so
        # ``function f case`` is a case; ``function case x in x)`` is a syntax error to
        # bash (the name IS ``case``), so its ``)`` closing the substitution is exact
        # (R29 GPT)
        assert _self_tokens('x "$(function f case a in a) :;; esac; :) g" y') == [
            "x",
            f"$(function f case a in a) :;; esac; :){_QUOTED_BLANK_MARK}g",
            "y",
        ]
        assert _self_tokens('x "$(coproc c case a in a) :;; esac; :) g" y') == [
            "x",
            f"$(coproc c case a in a) :;; esac; :){_QUOTED_BLANK_MARK}g",
            "y",
        ]
        # an ANSI-C body inside the quoted substitution is one span to BOTH readings:
        # the closer scan escapes through ``$'a\\')'`` as bash does, and the main pass
        # pops the enclosing quote even when its body scan stepped past the recorded
        # closer, so the quote state stays in step and the NEXT command is still split
        # into its own words (R32 Opus: ``; ssh -v localhost`` was glued into one token)
        tokens = _self_tokens("echo \"$(: $'a\\')b\\'c' )\"; ssh -v localhost")
        assert tokens[-3:] == ["ssh", "-v", "localhost"]
        assert _QUOTED_BLANK_MARK not in "".join(tokens)
        assert _self_tokens('x "$(echo function f case a in a) :;; esac) g" y') == [
            "x",
            f"$(echo function f case a in a){_QUOTED_BLANK_MARK}:;;{_QUOTED_BLANK_MARK}esac){_QUOTED_BLANK_MARK}g",
            "y",
        ]

    def test_a_quoted_redirect_character_is_text(self) -> None:
        from kiro_crew.security.shell_normalizer import _QUOTED_GT_MARK, _QUOTED_LT_MARK, _unmark

        assert _self_tokens('x "a>b" y') == ["x", f"a{_QUOTED_GT_MARK}b", "y"]
        assert _self_tokens("x 'a<b' y") == ["x", f"a{_QUOTED_LT_MARK}b", "y"]
        assert _self_tokens("x a>b y") == ["x", "a>b", "y"]
        assert _unmark(f"a{_QUOTED_GT_MARK}b{_QUOTED_LT_MARK}c") == "a>b<c"

    def test_a_quote_enclosing_a_substitution_keeps_its_blanks(self) -> None:
        assert _self_tokens('x "$(case y in y) :;; esac)" z') == [
            "x",
            "$(case y in y) :;; esac)",
            "z",
        ]
        assert _self_tokens('x "`a b`" z') == ["x", "`a b`", "z"]

    def test_the_walker_reads_the_word_whole(self) -> None:
        depth = _SubstitutionDepth()
        tokens = _self_tokens('k $(case "x y" in "x y") :;; esac; echo status) tok')[1:]
        for token in tokens:
            ended = depth.feed(token)
        assert ended is False and depth.top_level and depth.words == ["tok"]

    def test_the_mark_is_text_at_every_comparison(self) -> None:
        from kiro_crew.security.argv_floor import _unmask_separators
        from kiro_crew.security.shell_normalizer import _normalize_operand

        assert _unmask_separators(f"a{_QUOTED_BLANK_MARK}b") == "a b"
        assert _normalize_operand(f"'a{_QUOTED_BLANK_MARK}b'") == "a b"
        depth = _SubstitutionDepth()
        depth.feed(f"a{_QUOTED_BLANK_MARK}b")
        assert depth.words == [f"a{_QUOTED_BLANK_MARK}b"]  # consumers unmark
