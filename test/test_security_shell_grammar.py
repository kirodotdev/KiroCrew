"""Tests for kiro_crew.security: the shell grammar the self-protection gate reads.

Substitution bodies end where command grammar ends them, ``case`` arms only in command
position, and line continuations fold before tokenizing, inside substitution openers too.
"""

from __future__ import annotations

import pathlib

import pytest

# Pins the own-host probe (UDP route and own-hostname lookup) and the hosts-file layer
# local for every ``is_denied`` verdict in this module, as the denied-command suites do.
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


@pytest.fixture(autouse=True)
def _no_remote_host_is_looked_up(_floor_monkeypatch):
    """A glob at program position (``* "$(printf <verb>)"``) can be ``ssh``, so the gate
    asks DNS whether the next word is this machine. A test reaches no host but loopback,
    so that answer is fixed here, as ``test_deny_gate_call_sharing`` fixes it: not this
    machine."""
    _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)


class TestSubstitutionCloserReadsCommandGrammar:
    """A ``)`` that shell COMMAND GRAMMAR makes ordinary must not end a body.

    ``_substitution_bodies`` is the shared answer to "what text does this command
    run as a shell", so a body that stops early is not one pass's problem: every
    consumer inherits the blindness. Quoting was already handled -- the span walk
    reads ``_iter_shell_chars`` -- but three constructs put a literal ``)`` in front
    of that walk without quoting it, and each hid a payload this module refuses.

    The verb and the product name are assembled rather than spelled, because a
    literal pair of them in source order is itself matched by the regex tier and
    would mask what these cases are actually testing.

    Both directions are asserted. The scan may not stop early (the anchors), and
    it may not start refusing shapes it allows today -- a scanner made stricter
    in the wrong place is how a gate becomes unusable.
    """

    VERB = "tok" + "en"
    NAME = "kiro" + "crew"

    def test_a_comment_hides_the_closer_from_the_paren_count(self) -> None:
        """``$(: # )`` closes on the NEXT line, so the ``)`` after ``#`` is inert.

        The body came back as ``: # `` and the ``printf`` behind it -- which
        computes the credential-minting verb -- was never scanned, so the value
        assembled from it was not recognised and the command was allowed.
        """
        command = f"T=$(: # )\nprintf {self.VERB}); {self.NAME} $T"
        (body,) = security._substitution_bodies(command)
        assert f"printf {self.VERB}" in body, body
        assert security.is_denied(command) is not None

    def test_a_case_pattern_closer_is_not_the_substitutions(self) -> None:
        """In ``case x in x)`` the ``)`` terminates the PATTERN, not the body."""
        command = f"T=$(case x in x) printf {self.VERB};; esac); {self.NAME} $T"
        (body,) = security._substitution_bodies(command)
        assert f"printf {self.VERB}" in body, body
        assert security.is_denied(command) is not None

    @pytest.mark.parametrize(
        "word",
        [
            "${v:-)}",
            # A substitution inside the word holds its own ``}`` (bash-measured).
            "${v:-$(echo })}; : ${w:-)}",
            "${v:-<(echo })}; : ${w:-)}",
            "${v:-`echo }`; : )}",
        ],
    )
    def test_a_parameter_expansion_holds_its_own_parens(self, word: str) -> None:
        """``$(: ${v:-)}; ...)`` runs past the ``)`` in the default word, as bash does."""
        command = f"kill $(: {word}; pgrep -f {self.NAME})"
        (body,) = security._substitution_bodies(command)
        assert f"pgrep -f {self.NAME}" in body, body
        assert security.is_denied(command) is not None

    def test_an_unclosed_expansion_keeps_the_paren_only_span(self) -> None:
        """A ``${`` in here-document data never closes, so the paren-only span stands."""
        text = "$(cat <<'E'\n${\nE\n) echo hi"
        assert security._matching_close_paren(text, 2) == (text.index(")") + 1, True)

    def test_a_case_pattern_no_longer_truncates_a_self_kill_lookup(self) -> None:
        """The BODY is recovered for the self-kill spelling too.

        Only the body is asserted here. The verdict on this one does NOT flip,
        because the self-kill pass never attributes a substitution that sits in an
        ASSIGNMENT ahead of the ``kill`` -- a separate gap in that pass's
        attribution, which this span fix neither causes nor closes.
        """
        command = f"P=$(case x in x) pgrep -f {self.NAME};; esac); kill $P"
        (body,) = security._substitution_bodies(command)
        assert f"pgrep -f {self.NAME}" in body, body

    @pytest.mark.parametrize(
        "command",
        [
            # ``a#b`` is one ordinary word -- a ``#`` mid-word opens no comment.
            "echo $(printf a#b)",
            # ``esac`` handed to a command is an argument, not the reserved word.
            "echo $(printf esac) done",
            # ``lowercase`` merely ENDS in ``case``; it must not arm the rule.
            "echo $(printf lowercase)",
            # A ``(a|b)`` pattern is balanced-neutral inside the case.
            "echo $(case x in (a|b) printf hi;; esac)",
            "T=$(case x in x) printf hi;; esac); echo $T",
            "T=$(: # )\nprintf hi); echo $T",
            "echo $(printf %s ${v:-)}) done",
            # A ``$(`` in a ``${...}`` word inside a ``case`` closes on its own ``)``.
            "kill $(case x in a) echo ${P:-$(cat f)};; esac); echo kirocrew",
        ],
    )
    def test_the_new_grammar_does_not_start_refusing_benign_shapes(self, command: str) -> None:
        assert security.is_denied(command) is None

    def test_a_single_quoted_backtick_does_not_close_the_body(self) -> None:
        """The backtick closer reads the same state machine as the paren one.

        HARDENING: no refused payload was reachable through the old pairwise
        ``find``, because the strings it mis-read are ones bash itself rejects
        (backticks do not nest unescaped). It is fixed so the two spellings of the
        same closer cannot drift apart, which is how the paren half broke before.
        """
        (body,) = security._substitution_bodies("`A='`'; printf hi`")
        assert body == "A='`'; printf hi", body

    def test_a_double_quoted_backtick_still_closes_it(self) -> None:
        """``"`cmd`"`` runs ``cmd``, so a backtick in DOUBLE quotes is a real closer."""
        assert security._substitution_bodies('`printf "hi"`') == ['printf "hi"']

    def test_a_line_continuated_case_still_arms_the_pattern_rule(self) -> None:
        """``ca\\`` + newline + ``se`` IS ``case`` -- the shell folds it before reading words.

        Byte-literal recognition missed this spelling, so the rule never armed and
        the pattern's ``)`` closed the body early. bash was measured running the
        folded form as ``case``, so the body must survive it.

        The verdict is asserted too: ``_self_tokens`` folds the backslash-newline
        away before tokenizing, so the assignment and the invocation stay in one
        command, ``$T`` resolves, and the two halves together deny this spelling.
        The tokenizer-level cases live in
        ``TestSelfTokensFoldLineContinuations``.
        """
        command = f"T=$(ca\\\nse x in x) printf {self.VERB};; esac); {self.NAME} $T"
        (body,) = security._substitution_bodies(command)
        assert f"printf {self.VERB}" in body, body
        assert security.is_denied(command) is not None

    def test_a_line_continuated_esac_still_ends_the_pattern_rule(self) -> None:
        """The same folding applies to ``esac``, so the rule disarms where bash does."""
        (body,) = security._substitution_bodies("$(case x in x) printf hi;; es\\\nac)")
        assert body == "case x in x) printf hi;; es\\\nac", body

    def test_a_folded_continuation_does_not_make_a_hash_a_comment(self) -> None:
        """``a\\`` + newline + ``#b`` folds to the single word ``a#b``, which comments nothing."""
        (body,) = security._substitution_bodies("$(printf a\\\n#b)")
        assert body == "printf a\\\n#b", body

    def test_a_fold_after_a_word_break_still_opens_a_comment(self) -> None:
        """What matters is what the fold leaves ADJACENT, not that a fold is there.

        ``:`` + space + ``\\`` + newline + ``#`` folds to ``: #``, so a word break ends
        up in front of the ``#`` and bash opens a real comment. Reading any preceding
        fold as "not a comment" missed it in the fail-OPEN direction: the walk then
        read the ``)`` the comment hides as the closer and truncated the body before
        the verb, reopening this PR's own bypass for the folded spelling.
        """
        command = f"T=$(: \\\n# )\nprintf {self.VERB}); {self.NAME} $T"
        (body,) = security._substitution_bodies(command)
        assert f"printf {self.VERB}" in body, body
        assert security.is_denied(command) is not None

    @pytest.mark.parametrize(
        "text",
        ["$(", "`", "$(case", "$(case x in x", "$(: #", "`A='", "$(#", "", "#", "esac)"],
    )
    def test_degenerate_input_does_not_raise(self, text: str) -> None:
        """An unterminated construct yields the remainder, never an exception."""
        assert isinstance(security._substitution_bodies(text), list)

    def test_an_unproven_span_still_yields_the_whole_remainder(self) -> None:
        """Fail-CLOSED direction: a body that reaches too far is only over-scanned."""
        (body,) = security._substitution_bodies(f"$(case x in x) printf {self.VERB}")
        assert f"printf {self.VERB}" in body, body


class TestCaseArmingRequiresCommandPosition:
    """``case`` is a reserved word only in COMMAND POSITION, so the
    pattern-paren rule must not arm on the word as data. Every vector is
    bash-verified live: the non-arming forms are ones bash reads as data (or
    refuses outright), and the arming forms are ones bash spans."""

    @pytest.mark.parametrize(
        ("command", "body"),
        [
            # ``case`` as an ARGUMENT: bash closes at the first unquoted ``)``.
            ("echo $(echo case x in y) tail", "echo case x in y"),
            ("echo $(echo then case x in y) tail", "echo then case x in y"),
            # A quoted spelling is data even in command position.
            ("echo $('case' x in y) tail", "'case' x in y"),
            # An assignment prefix removes command position (bash: syntax
            # error at the pattern paren, the line never parses).
            ("echo $(v=1 case x in y) tail", "v=1 case x in y"),
            # ``command case`` / ``eval case``: operands, not the keyword.
            ("echo $(command case x in y) tail", "command case x in y"),
        ],
    )
    def test_case_as_data_no_longer_over_arms(self, command: str, body: str) -> None:
        assert security._substitution_bodies(command) == [body]
        assert security.is_denied(command) is None

    @pytest.mark.parametrize(
        "command",
        [
            # Command position hands through reserved words -- INHERITED, so
            # each keeper must hold position itself.
            "kill $(if true; then case x in x) : ;; esac; fi; pgrep -f kirocrew)",
            "kill $({ case x in x) : ;; esac; }; pgrep -f kirocrew)",
            # Both function-definition forms make the body command position.
            "kill $(function f case x in x) : ;; esac; pgrep -f kirocrew)",
            "kill $(f() case x in x) : ;; esac; f; pgrep -f kirocrew)",
            "kill $(f ( ) case x in x) : ;; esac; f; pgrep -f kirocrew)",
            # ``coproc [NAME] compound-command``.
            "kill $(coproc c case x in x) : ;; esac; pgrep -f kirocrew)",
            # A nested case in a clause body opens after ``)`` -> command position.
            "kill $(case a in a) case b in b) : ;; esac ;; esac; pgrep -f kirocrew)",
            # The MULTI-CLAUSE form: an outer pattern paren
            # AFTER the inner esac is what a missed inner arm hands to the
            # desynchronised counter as the closer -- this vector is red on a
            # gate that skips the clause-body position.
            "kill $(case a in a) case b in b) : ;; esac ;; c) : ;; esac; pgrep -f kirocrew)",
            # ``case`` GLUED to a backtick opener: the
            # opener both breaks the word and starts a command, so the word
            # must still be recognised and armed.
            "kill $(`case x in x) : ;; esac`; pgrep -f kirocrew)",
        ],
    )
    def test_every_bash_spanning_form_still_arms(self, command: str) -> None:
        (body,) = security._substitution_bodies(command)
        assert "pgrep -f kirocrew" in body, body
        assert security._is_self_kill(command)

    def test_a_redirect_prefixed_case_is_not_armed(self) -> None:
        """bash REFUSES a redirect prefix before a compound command (measured:
        syntax error, the line never runs), so the exact reading closes at the
        pattern paren rather than spanning a command that cannot execute."""
        body, *_rest = security._substitution_bodies("echo $( > f case x in y) tail")
        assert body == " > f case x in y", body

    def test_esac_after_in_stays_safe_but_unproven(self) -> None:
        """``$(case z in esac; echo after)`` -- bash ends the case at that
        ``esac``, but disarming there needs subject/``in`` state the flat
        counter deliberately does not carry: any cheaper rule (e.g. 'esac
        after the word in') disarms on ``echo in esac`` inside a clause body,
        which is the BYPASS direction. Pinned to the fail-closed fallback:
        unproven span, whole remainder, extractors scan more."""
        (body,) = security._substitution_bodies("$(case z in esac; echo after)")
        assert "echo after" in body, body

    def test_option_words_are_transparent_in_the_chain(self) -> None:
        """Option words pass the decision through to what precedes them.

        ``time -p case`` / ``time -- case``: bash's OWN substitution parser
        refuses these spellings (measured live: syntax error, the tail prints
        as literal text and never executes), so no executable bypass exists
        either way -- but the grammar reads the reserved word there, and
        arming keeps the walk uniform with the bare ``time case`` form at the
        cost of a longer span on input bash never runs (the documented safe
        direction). ``echo -n case`` chains to ``echo`` and stays data."""
        from kiro_crew.security.shell_normalizer import _matching_close_paren

        for text in (
            "$(time -p case x in x) : ;; esac; pgrep -f kirocrew)",
            "$(time -- case x in x) : ;; esac; pgrep -f kirocrew)",
        ):
            assert _matching_close_paren(text, 2) == (len(text), True), text
        assert security._substitution_bodies("echo $(echo -n case x in y) tail") == [
            "echo -n case x in y"
        ]

    def test_heredoc_bodies_extract_whole_and_the_consumer_convicts(self) -> None:
        """A ``)`` inside heredoc DATA must not end the extracted body early.

        These pins hold on this branch and on the base alike: the span is
        unproven through the heredoc, so the extractor falls back to the whole
        remainder (scan more, never less) and the payload after the heredoc IS
        scanned — the self-kill consumer convicts. bash runs the tail in both
        shapes (verified live)."""
        for cmd in (
            "kill $(echo <<X\ncase x in y)\nX\npgrep -f kirocrew)",
            "kill $(cat <<X\ncase a in a) : ;; esac\nX\npgrep -f kirocrew)",
            # A ``${`` in the data: the paren-only span is kept, never a shorter one.
            "kill $(cat <<X\n${\nX\n(echo })\npgrep -f kirocrew)",
        ):
            (body,) = security._substitution_bodies(cmd)
            assert "pgrep -f kirocrew" in body, body
            assert security._is_self_kill(cmd)

    def test_the_two_keyword_tables_are_cross_pinned(self) -> None:
        """The command-position keepers and ``_SHELL_RESERVED_WORDS`` answer
        the same 'is this word shell syntax?' question for different purposes
        (grammar model here, fail-closed bail in the redirect skip). Every
        membership difference is intentional and named, so an edit to one
        table trips this pin and the editor rules on the other deliberately."""
        from kiro_crew.security.argv_floor import _SHELL_RESERVED_WORDS
        from kiro_crew.security.shell_normalizer import _KEEPS_COMMAND_POSITION

        only_reserved = _SHELL_RESERVED_WORDS - _KEEPS_COMMAND_POSITION
        only_keeps = _KEEPS_COMMAND_POSITION - _SHELL_RESERVED_WORDS
        assert only_reserved == {
            # handled structurally by the span walk, not as position-keepers:
            "case",  # arms the pattern rule (command position gated)
            "esac",  # disarms it (command position gated)
            "in",  # case grammar, never hands position on
            "function",  # name-consuming prefix, chained in _arms_case_context
            # loop/conditional heads whose operands are NOT command position;
            # their bodies re-enter it via do/then, which ARE in the set:
            "for",
            "select",
            # bracket commands whose operands are test expressions:
            "[[",
            "]]",
            # block ENDERS: bash refuses a keyword directly after each
            # (``fi case`` / ``done case`` / ``} case`` are syntax errors,
            # measured), so none of them hands command position on:
            "fi",
            "done",
            "}",
        }
        assert only_keeps == set()

    def test_block_enders_do_not_hand_position_on(self) -> None:
        """``fi case`` / ``done case`` are bash SYNTAX ERRORS (measured), so
        the exact reading closes at the first paren rather than spanning a
        line that can never run."""
        for command, body in (
            ("echo $(if true; then :; fi case x in y) tail", "if true; then :; fi case x in y"),
            (
                "echo $(for i in 1; do :; done case x in y) tail",
                "for i in 1; do :; done case x in y",
            ),
        ):
            assert security._substitution_bodies(command)[0] == body, command

    def test_a_continuation_split_keeper_still_arms(self) -> None:
        """A keeper split by a line continuation hands command position on.

        bash removes ``\\`` + newline while READING, so ``th\\`` + newline +
        ``en case x in x) ...`` runs as ``then case ...`` (measured). The
        backward word parser must join across the continuation the same way:
        stopping at the raw newline reads the fragment ``en``, refuses to arm,
        and the pattern ``)`` then closes substitution scanning early -- the
        under-scan direction the self-protection consumers cannot afford.
        """
        from kiro_crew.security.shell_normalizer import _arms_case_context

        for keeper_split in ("th\\\nen", "i\\\nf true; then", "d\\\no"):
            head = {
                "th\\\nen": f"if true; {keeper_split}",
                "i\\\nf true; then": keeper_split,
                "d\\\no": f"while true; {keeper_split}",
            }[keeper_split]
            text = f"{head} case x in x) echo BODY;; esac"
            idx = text.rindex("case")
            assert _arms_case_context(text, idx), text

    def test_a_continuation_split_keeper_spans_the_whole_body(self) -> None:
        """The substitution body survives the pattern ``)`` when the keeper is split."""
        command = 'kill -9 $(if true; th\\\nen case x in x) pgrep -f "kiro""crew";; esac; fi)'
        (body,) = security._substitution_bodies(command)
        assert 'pgrep -f "kiro""crew"' in body, body
        assert body.endswith("fi"), body
        assert security.is_denied(command) is not None

    def test_a_split_keeper_word_is_joined_not_fragmented(self) -> None:
        """``_prev_word`` reads ``th\\`` + newline + ``en`` as one word.

        The joined word must carry its continuation glue (so the arming walk
        can classify it) and start at the FIRST fragment, so chained walks
        (``then`` -> ``if``) resume before the whole keeper, not mid-word.
        """
        from kiro_crew.security.shell_normalizer import _prev_word

        text = "th\\\nen case"
        got = _prev_word(text, text.index("case"))
        assert got is not None
        word, start = got
        assert word.replace("\\\n", "") == "then", got
        assert start == 0, got

    def test_a_keeper_word_as_function_name_still_arms(self) -> None:
        """``function time case ...``: ``time`` is the definition NAME, not the keyword.

        bash accepts any reserved word as a function name after ``function``
        (``function do`` / ``function if`` parse, measured), and the definition
        body is command position -- so the ``case`` there is bash's reserved
        word and must arm.  The name-prefix check has to run BEFORE keeper
        semantics: reading ``time`` as the keeper chains to ``function``, which
        keeps nothing, and the refused arm truncates the substitution body at
        the pattern ``)`` -- the under-scan direction.
        """
        from kiro_crew.security.shell_normalizer import _arms_case_context

        for text in (
            "function time case",
            "function do case",
            "function if case",
            "coproc time case",
        ):
            assert _arms_case_context(text, text.rindex("case")), text

    def test_a_function_named_keeper_body_spans_and_convicts(self) -> None:
        """The review vector: the pgrep under a function-named keeper is scanned.

        bash defines the function (never runs it) and the substitution result
        still reaches the outer command (measured), so a truncated body hides
        the pgrep from the scan while bash evaluates it.  The clause-position
        spelling both spans and convicts.  The esac-tail spelling
        (``... :;; esac; pgrep ...``) is pinned for SPAN only: its conviction
        depends on how the consumer segments a command list after ``esac``,
        which behaves the same with this gate present or absent (identical on
        the base branch, measured) and is tracked separately.
        """
        clause = 'kill -9 $(function time case x in x) pgrep -f "kiro""crew";; esac)'
        (body,) = security._substitution_bodies(clause)
        assert 'pgrep -f "kiro""crew"' in body, body
        assert security.is_denied(clause) is not None

        tail = 'kill -9 $(function time case x in x) :;; esac; pgrep -f "kiro""crew")'
        (body,) = security._substitution_bodies(tail)
        assert 'pgrep -f "kiro""crew"' in body, body


class TestSelfTokensFoldLineContinuations:
    """``_self_tokens`` folds ``\\`` + newline away BEFORE tokenizing.

    The shell removes a line continuation while READING, before it tokenizes
    anything, so the two characters vanish. ``_self_tokens`` folds them the same
    way, which keeps an assignment and the invocation it feeds in ONE command:
    ``$T`` resolves and the ``kirocrew token`` argv pair forms, so the
    self-protection check sees the command bash assembles and runs. A quote-blind
    fold is safe here because this view only feeds the deny-direction
    self-protection predicates.

    Both directions are asserted: the continuation spelling matches its folded
    twin, and a REAL unescaped newline -- which the shell keeps as a separator --
    keeps its verdict on both the benign and the denied side.
    """

    VERB = "tok" + "en"
    NAME = "kiro" + "crew"

    def test_a_continuation_is_folded_not_read_as_a_separator(self) -> None:
        """The token list carries no ``';'`` -- the pair vanishes, as in bash."""
        command = f"T=$(ca\\\nse x in x) printf {self.VERB};; esac); {self.NAME} $T"
        tokens = security._self_tokens(command.lower())
        assert ";" not in tokens, tokens
        assert tokens[0] == "t=$(case", tokens

    def test_the_assignment_then_invoke_continuation_spelling_is_denied(self) -> None:
        """The documented assignment-then-invoke shape denies once the fold keeps the pair together."""
        command = f"T=$(ca\\\nse x in x) printf {self.VERB};; esac); {self.NAME} $T"
        assert security.is_denied(command) is not None

    def test_the_folded_spelling_is_denied_alongside_the_plain_one(self) -> None:
        """The continuation spelling and its folded twin get the same verdict."""
        folded = f"T=$(case x in x) printf {self.VERB};; esac); {self.NAME} $T"
        plain = f"{self.NAME} {self.VERB}"
        assert security.is_denied(folded) is not None
        assert security.is_denied(plain) is not None

    def test_a_continuation_inside_the_program_name_still_denies(self) -> None:
        """``kiro\\`` + newline + ``crew token`` is one word to bash, so it denies."""
        assert security.is_denied(f"{self.NAME[:4]}\\\n{self.NAME[4:]} {self.VERB}") is not None

    def test_a_continuation_inside_a_module_name_reaches_the_floor(self) -> None:
        """The cheap floor prefilter must fold before it decides it cannot fire.

        ``_self_floor_can_fire`` strips removable quote and backslash glue before
        looking for the product name. A ``\\`` + newline leaves a newline behind
        once the backslash goes, which pushes the two halves of ``kiro_crew`` past
        the hint's one-separator budget -- so the gate answered "provably cannot
        fire" and the whole argv scan was skipped for a command bash mints from.
        """
        command = f"python -m {self.NAME[:4]}\\\n_crew {self.VERB}"
        assert security.argv_floor._self_floor_can_fire(command) is True
        assert security.is_denied(command) is not None

    def test_an_even_backslash_run_is_not_a_continuation(self) -> None:
        """``\\\\`` before a newline is an escaped literal backslash, so the line ENDS.

        Only a LONE ``\\`` + newline continues a line. Bash runs a mint on the
        second line of ``true\\\\`` + newline + ``python -m kirocrew token``; a
        fold that joined any backslash-newline would mangle the ``python`` token
        and hide the mint, so the fold must leave the even run intact and the
        mint on its own line must still deny.
        """
        command = f"true\\\\\npython -m {self.NAME} {self.VERB}"
        assert security.is_denied(command) is not None

    def test_a_backslash_crlf_is_not_a_continuation(self) -> None:
        """``\\`` + CRLF is NOT a continuation: bash escapes the CR and the LF ends the line.

        Measured against bash: ``echo a\\`` + CRLF + ``echo b`` runs two
        commands. So ``true\\`` + CRLF + ``python -m kirocrew token`` runs the
        mint on the second line, and folding the ``\\`` + CRLF away would join
        ``true`` and ``python`` and hide the mint from the argv check.
        """
        command = f"true\\\r\npython -m {self.NAME} {self.VERB}"
        assert security.is_denied(command) is not None

    def test_an_even_backslash_run_before_crlf_is_not_a_continuation(self) -> None:
        """``\\\\`` + CRLF ends the line just as ``\\\\`` + LF does; the mint on line two denies."""
        command = f"true\\\\\r\npython -m {self.NAME} {self.VERB}"
        assert security.is_denied(command) is not None

    def test_a_real_newline_still_separates_commands(self) -> None:
        """An UNESCAPED newline is a separator; folding must not touch it.

        Pins the TOKEN view, not just the verdict: a continuation folds two words
        into one (``echo a\\<newline>b`` -> ``echo ab``) while a real newline starts
        another command (``echo a<newline>b`` -> ``echo a; b``). The separator
        must survive tokenization. Asserting only ``is_denied`` here would pass even
        if the fold ate real newlines too, since neither command is a mint.
        """
        assert security._self_tokens("echo a\\\nb") == ["echo", "ab"]
        assert security._self_tokens("echo a\nb") == ["echo", "a", ";", "b"]
        assert security.is_denied("echo a\necho b") is None

    def test_a_real_newline_mint_keeps_its_denial(self) -> None:
        """A mint on its own line after a real newline stays denied."""
        assert security.is_denied(f"echo a\n{self.NAME} {self.VERB}") is not None

    def test_a_benign_continuation_stays_allowed(self) -> None:
        """Folding shapes the argv only -- a harmless command must not start refusing."""
        assert security.is_denied("echo a\\\nb") is None

    def test_a_quoted_separator_still_stays_inside_its_token(self) -> None:
        """The fold must not disturb shlex's quote resolution downstream."""
        tokens = security._self_tokens(f"pkill -f '[;]*{self.NAME}'")
        assert f"[;]*{self.NAME}" in tokens, tokens


class TestSubstitutionBodiesReadFoldedOpeners:
    """A ``\\`` + newline inside a substitution opener does not hide the body.

    The shell removes a line continuation while READING, before it lexes an
    opener, so ``cat <\\`` + newline + ``(...)`` is a process substitution to bash
    (measured: ``cat <\\<newline>(echo hi)`` prints ``hi``; the ``>\\<newline>(``
    and ``$\\<newline>(`` spellings run their bodies the same way).
    ``_substitution_bodies`` recognises its openers byte-literally, so handing it
    the RAW source extracts no body for a continuation-split opener and the inner
    command goes unscanned: ``cat <\\<newline>(bash -c '<name> <verb>')`` reads
    as ALLOWED while bash runs the mint. The payload walk therefore reads the
    bodies from the same quote-aware fold the tokenizer applies, so the two views
    agree.

    The matrix is the three parenthesised openers x (split opener, split program,
    split verb), each asserted against its unsplit twin, plus the CRLF, real
    newline, single-quoted and ANSI-C spellings that must NOT change verdict.
    """

    VERB = "tok" + "en"
    NAME = "kiro" + "crew"
    OPENERS = ("<(", ">(", "$(")

    @classmethod
    def _mint(cls) -> str:
        return f"{cls.NAME} {cls.VERB}"

    @pytest.mark.parametrize("opener", OPENERS)
    def test_the_unsplit_spelling_is_denied(self, opener: str) -> None:
        """Baseline: the plain spelling of each opener is already refused."""
        assert security.is_denied(f"cat {opener}{self._mint()})") is not None

    @pytest.mark.parametrize("opener", OPENERS)
    def test_a_continuation_inside_the_opener_is_folded(self, opener: str) -> None:
        """``<\\`` + newline + ``(`` opens a substitution; the body is walked.

        The body is a wrapper (``bash -c``) so the inner mint is reachable ONLY
        through the substitution-body walk -- the top-level argv sees one opaque
        token. A raw-source body scan reads this spelling as ALLOWED.
        """
        split = f"cat {opener[0]}\\\n({'bash -c'} '{self._mint()}')"
        plain = f"cat {opener}bash -c '{self._mint()}')"
        assert security.is_denied(plain) is not None
        assert security.is_denied(split) is not None

    @pytest.mark.parametrize("opener", OPENERS)
    def test_a_continuation_inside_the_opener_reaches_the_payload_walk(self, opener: str) -> None:
        """Pins the WALK, not just the verdict: the body appears as its own frame."""
        command = f"cat {opener[0]}\\\n(bash -c '{self._mint()}')".lower()
        sources = [source for source, _tokens in security._shell_payload_walk(command)]
        assert any(self._mint() in source and "cat" not in source for source in sources), sources

    @pytest.mark.parametrize("opener", OPENERS)
    def test_a_continuation_inside_the_program_name_is_denied(self, opener: str) -> None:
        """``<(kiro\\`` + newline + ``crew <verb>)`` is one program word to bash."""
        split = f"cat {opener}{self.NAME[:4]}\\\n{self.NAME[4:]} {self.VERB})"
        assert security.is_denied(split) is not None

    @pytest.mark.parametrize("opener", OPENERS)
    def test_a_continuation_inside_the_verb_is_denied(self, opener: str) -> None:
        """``<(<name> tok\\`` + newline + ``en)`` is one verb word to bash."""
        split = f"cat {opener}{self.NAME} {self.VERB[:3]}\\\n{self.VERB[3:]})"
        assert security.is_denied(split) is not None

    @pytest.mark.parametrize("opener", OPENERS)
    def test_a_split_opener_around_a_computed_verb_is_denied(self, opener: str) -> None:
        """The assignment-then-invoke body is only visible once the body is walked."""
        body = f"T=$(printf {self.VERB}); {self.NAME} $T"
        assert security.is_denied(f"cat {opener}{body})") is not None
        assert security.is_denied(f"cat {opener[0]}\\\n({body})") is not None

    def test_a_split_opener_nested_inside_another_substitution_is_denied(self) -> None:
        """The fold applies at every depth of the walk, not only at the top frame."""
        command = f"echo $(cat <\\\n(bash -c '{self._mint()}'))"
        assert security.is_denied(command) is not None

    @pytest.mark.parametrize("opener", OPENERS)
    def test_a_backslash_crlf_inside_the_opener_is_not_a_continuation(self, opener: str) -> None:
        """``\\`` + CRLF is NOT folded: bash escapes the CR and the LF ends the line.

        Measured against bash, the CRLF spelling of the opener does not form a
        substitution, so it is not a mint. The verdict must match the REAL
        newline spelling of the same text, in both directions: neither is refused
        for a benign body, and a mint on the line AFTER the CR stays denied
        because that line runs on its own.
        """
        crlf = f"cat {opener[0]}\\\r\n(echo hi)"
        real = f"cat {opener[0]}\n(echo hi)"
        assert security.is_denied(crlf) is None
        assert security.is_denied(real) is None
        assert security.is_denied(f"cat {opener[0]}\\\r\n{self._mint()}") is not None

    @pytest.mark.parametrize("opener", OPENERS)
    def test_a_backslash_crlf_inside_the_body_is_not_folded(self, opener: str) -> None:
        """``<(kiro\\`` + CRLF + ``crew <verb>)`` runs two commands inside the body.

        Bash runs ``kiro<CR>`` (not found) and then ``crew <verb>`` -- neither is
        the mint -- so the folded word must not be fabricated. Only the verdict on
        a body that DOES mint on its second line is pinned in the deny direction.
        """
        two_lines = f"cat {opener}true\\\r\n{self._mint()})"
        assert security.is_denied(two_lines) is not None
        folded_body = security._substitution_bodies(
            security._fold_line_continuations(
                f"cat {opener}{self.NAME[:4]}\\\r\n{self.NAME[4:]} x)"
            )
        )
        assert folded_body == [f"{self.NAME[:4]}\\\r\n{self.NAME[4:]} x"], folded_body

    def test_a_real_newline_inside_the_body_keeps_its_verdict(self) -> None:
        """An UNESCAPED newline is a separator inside a body too; the fold leaves it."""
        assert security.is_denied("cat <(echo a\necho b)") is None
        assert security.is_denied(f"cat <(echo a\n{self._mint()})") is not None

    def test_a_single_quoted_continuation_inside_the_body_is_literal(self) -> None:
        """Single quotes keep ``\\`` + newline literal, so no mint is fabricated."""
        assert (
            security.is_denied(f"cat <(echo '{self.NAME[:4]}\\\n{self.NAME[4:]} {self.VERB}')")
            is None
        )

    def test_an_ansi_c_continuation_inside_the_body_is_literal(self) -> None:
        """``$'…'`` keeps ``\\`` + newline literal, so no mint is fabricated."""
        assert (
            security.is_denied(f"cat <(echo $'{self.NAME[:4]}\\\n{self.NAME[4:]} {self.VERB}')")
            is None
        )

    def test_the_ansi_c_publish_literal_stays_allowed(self) -> None:
        """The benign case the issue measured flipping under a seed-level fold stays allowed.

        Folding at the body walk, rather than at the walk's SEED, cannot reshape
        text outside a substitution, and the fold it uses preserves ANSI-C spans.
        """
        command = "echo bash -c $'g\\'it\\\n\\' push origin main'"
        assert security.is_denied(command) is None


_V = "tok" + "en"
_N = "kiro" + "crew"
_SENT_AWAY = ">/tmp/f >>/tmp/f >|/tmp/f >&2 1>&2 &>/tmp/f >/dev/null".split() + [
    "> /tmp/f",
    "3>/tmp/f 1>&3",
    ">& /tmp/f",
]
_DIGEST = "; ".join(f"{v}=$({_N} status --json | jq -r .{v.lower()})" for v in "ABCDEFGHI")
_NOT_READ = "verb word inside a substitution; its output is not read"


class TestMintVerbFromAQuotedSubstitution:
    """A substitution that names the mint verb mints, wherever its output would go.

    What a substitution prints is outside this static floor, so the verb's PRESENCE
    binds: an assigned value whose substitution names it is the verb (and the program,
    when it names that too), and a quoted ``"$(...)"`` that names it where the
    subcommand stands mints.  The floor is asserted directly as well as through
    ``is_denied``, because the catalog rows refuse some of these spellings and would
    mask a floor miss.
    """

    @pytest.mark.parametrize(
        "command",
        [
            # The issue's rows: a quote trick inside the assigned substitution.
            f'T=$(: "\'"; echo {_V}); {_N} $T',
            f'T=$(echo "it\'s" >/dev/null; echo {_V}); {_N} $T',
            f'T=$(: "\'"; echo {_V}); {_N} ${{T}}',
            f'T=$(: "\'"; echo {_V}); {_N} $T; echo done',
            f'T=$(: "\'"; echo {_V}); {_N} $T|cat',
            f'T=$(: "\'"; echo {_V}); {_N} ${{T:0}}',
            f'T=$(: "\'"; echo {_V}); {_N} ${{T/a/b}}',
            f'T=$(: "\'"; echo {_V}); U=$T; {_N} $U',
            f'T=$(: "\'"; echo {_V}); export U=$T; {_N} $U',
            f'T=$(: "\'"; echo {_V}); {_N} $(echo $T)',
            f'T=$(: "\'"; echo {_V}); W=T; {_N} ${{!W}}',
            f'T=$(: "\'"; echo {_V}); bash -c "{_N} $T"',
            f'T=$(: "\'"; echo {_V}); bash -c "{_N} ${{T:0}}; echo x"',
            f'T=$(: "\'"; echo {_V}); echo "{_N} $T" | bash',
            f'T=${{U:+"\'"}}$(printf {_V}); {_N} $T',
            f'T=${{U-"\'"}}$(printf {_V}); {_N} $T',
            f'T=$(: "\'"; echo {_V} | cat); {_N} $T',
            f'T=$(: "\'"; command printf {_V}); {_N} $T',
            f'T=$(: "\'"; echo {_V}); {_N} chat; {_N} $T',
            # "Protected program wins": when the body names a protected program the
            # value binds to that program, so a later use as the program mints.  A DECOY
            # verb placed BEFORE the program in the body cannot flip that -- the body
            # still names the program -- which closes the bypass a first-name read left
            # open (``$T token`` resolving to ``token token``).
            f'P=$(: "\'"; echo {_N}); V=$(: "\'"; echo {_V}); $P $V; echo {_N}',
            f'T=$(: "\'"; echo {_N}); echo $($T {_V})',
            f'T=$(: "\'"; echo {_N}); {_V}=1; $T {_V}',
            f'T=$(: "\'"; : {_V}; echo {_N}); $T {_V}',
            f'T=$(: "it\'s"; : {_V}; echo {_N}); $T {_V}',
            f'T=$(: "\'"; echo {_V} >/dev/null; echo {_N}); $T {_V}',
            # A lettered glob that bash expands to the product binds the program too
            # (the 06:21 glob rule), so ``$T <verb>`` still refuses.
            f"T=$(: \"'\"; echo 'kirocre[w]'); $T {_V}",
            f'T=$(: "\'"; echo k*); $T {_V}',
            # A later command's apostrophe (``echo "it's done"``) is irrelevant: the
            # alternative reads only the substitution's OWN body, which here prints the
            # verb, so both spellings are refused (the gpt/Opus r17 regression), while a
            # balanced body that names nothing stays allowed (see MAIN_RESIDUALS).
            f'T=`printf %s {_V}`; {_N} $T; echo "it\'s done"',
            f'T=$(printf %s {_V}); {_N} $T; echo "it\'s done"',
            # A quoted ``)`` inside the body, or a backtick body: the body still prints
            # the verb (``$(: ")"; printf token)`` runs ``printf token``; the backtick
            # body likewise), so bash mints.  The raw-body reader reads past the quoted
            # ``)`` and closes the backtick, refusing both -- the residual the paren
            # count left (gpt F1's quoted-``)`` class) is closed.
            f'T=$(: ")"; printf {_V}); {_N} $T',
            f'T=`: "\'"; echo {_V}`; {_N} $T',
            # Each assignment takes its own reading: a later one that names the verb
            # binds it, and a use before a benign reassignment still sees the verb.
            f'T=$(echo status); T=$(: "\'"; echo {_V}); {_N} $T',
            f'T=$(: "\'"; echo {_V}); {_N} $T; T=$(echo status)',
            f'(T=$(echo hi)); T=$(: "\'"; echo {_V}); {_N} $T',
            # A lettered glob in the body still spells the product (opus r19 keep-pin).
            f"F=$(ls kiro*); $F {_V}",
            # An ALIAS of a program-bound variable, used AT program position, still mints
            # (gpt r20 F3b): the program-only provenance only gates OPERAND uses, so
            # ``$U`` as argv[0] resolves and the verb after it is the mint.
            f"P=$(echo {_N}); U=$P; $U {_V}",
            f"F=$(ls kiro*); U=$F; $U {_V}",
            # An ALIAS of a verb-bound variable is NOT program-only, so it resolves at the
            # mint's operand as before: ``U=$T`` then ``<name> $U`` is the verb.
            f'T=$(: "\'"; echo {_V}); U=$T; {_N} $U',
            # The alias RHS may itself be a transform/indirect reference; at program
            # position (or as the mint's operand) it still resolves and mints (r20 F3c).
            f"TESTS=$(git ls-files 'test/*crew*'); U=${{TESTS:0}}; $U {_V}",
            f'T=$(: "\'"; echo {_V}); U=${{T:0}}; {_N} "$U"',
            # A brace expansion closed in its own word, or a function defined before
            # the assignment, does not hide the frame-scope assignment after it.
            f'echo {{a,b}}; T=$(: "\'"; echo {_V}); {_N} $T',
            f'f(){{ :; }}; T=$(: "\'"; echo {_V}); {_N} $T',
            # A ``)`` in a redirect target must not leave the argv scan's depth negative
            # (opus r20): the ``$(true;`` after it would lift it only to zero and end
            # the scan at its ``;`` before ``token)`` is read.  Main refuses this.
            f'{_N} 2>"x)" $(true; echo {_V})',
            # A quoted substitution where the subcommand stands.
            f'{_N} "$(printf {_V})"',
            f'{_N} "`printf {_V}`"',
            f'python -m kiro_crew "$(printf {_V})"',
            f'python -X dev -m kiro_crew "$(printf {_V})"',
            f'python -W ignore -m kiro_crew "$(printf {_V})"',
            f'{_N} >/dev/null "$(printf {_V})"',
            f'{_N} > /tmp/mint-url "$(printf {_V})"',
            f'{_N} > "$(printf x)" "$(printf {_V})"',
            # A redirect operator before the subcommand (``>|``, ``{{fd}}>``) is not it.
            f'{_N} > f "$(printf {_V})"',
            f'{_N} >| f "$(printf {_V})"',
            f'{_N} 2>> f "$(printf {_V})"',
            f'{_N} {{fd}}> f "$(printf {_V})"',
            # A redirection LEADING the command is not argv[0] either -- the shell strips
            # it and still runs the product CLI -- so the quoted-substitution reading fires
            # (opus r20 F3 keep-pin), as it does for the same redirect written after the CLI.
            f'>/dev/null {_N} "$(printf {_V})"',
            f'2>/dev/null {_N} "$(printf {_V})"',
            f'> /dev/null {_N} "$(printf {_V})"',
            # A redirect target that is itself a substitution is not the subcommand;
            # the ``;`` inside it does not end the argv, so the verb after is read.
            f"{_N} >$(true ; echo /tmp/f) {_V}",
            f"{_N} 2>$(true ; echo /tmp/e) {_V}",
            f"python -m kiro_crew >$(true ; echo /tmp/f) {_V}",
            # bash prints the verb written as a redirect-glued word inside the
            # substitution; the floor reads it where the quoted substitution stands.
            f'python -m kiro_crew "$(echo {_V}>&1)"',
            # Behind a glob that spells some of the product's name, as main reads it.
            f"k* $(printf {_V})",
            f'k* "$(printf {_V})"',
            f'*crew "$(printf {_V})"',
            f'kirocre[w] "$(printf {_V})"',
            f"{_N} $(printf {_V})",
            # A decoy ``T=`` the token walk does not read as an assignment (inside a
            # subshell, a substitution, or after an escaped blank) does not shadow the
            # real one; the real assignment's verb is still read.
            f"(T=$(echo hi)); T=$(echo {_V}); {_N} $T",
            f"echo $(T=$(date)); T=$(printf {_V}); {_N} $T",
            f"echo \\ T=$(date); T=$(printf {_V}); {_N} $T",
            # A decoy ``T=`date` `` inside quotes is skipped; the real assignment is read.
            f"echo ' T=`date`'; T=`echo {_V}`; {_N} $T",
            f'echo " T=`date`"; T=$(printf {_V}); {_N} $T',
            # A quoted opener (``T="$(...)"``) and a multi-substitution value are read.
            f'T="$(printf {_V})"; {_N} $T; echo T=$(date)',
            f"T=$(true)$(printf {_V}); {_N} $T",
            f'T=$(true)"$(printf {_V})"; {_N} $T',
            # A verb split by an empty substitution or a default still reads as the verb.
            f"T=$(printf tok$()en); {_N} $T",
            f"T=$(printf tok``en); {_N} $T",
            f"T=$(printf ${{X:-tok}}en); {_N} $T",
            f"T=$(echo tok$()en); {_N} $T",
            # A variable REBOUND to the mint verb loses any earlier program-only
            # provenance, so the operand gate resolves it and the mint is seen -- the
            # stale flag that let `kirocrew $P` run `kirocrew token` is discarded on
            # every rebinding (gpt r21 F1). The three-rebinding chain ending on the verb
            # is the same, through two intervening readings.
            f'P=$(echo {_N}); P=$(: "\'"; echo {_V}); {_N} $P',
            f'P=$(echo {_N}); P=$(echo doctor); P=$(: "\'"; echo {_V}); {_N} $P',
            # A leading redirection the shell strips from argv still leaves the
            # substitution-valued assignment at the command's prefix, so the body is read
            # and the mint refused. The prefix test walks the same redirect-aware walk as
            # `_program_positions`, so an fd-prefixed (`2>`), fd-dup (`1>&2`), combined
            # (`&>`) or brace-fd (`{fd}>`) redirect -- none of which name a program --
            # does not hide the assignment the way a redirect-blind program read would.
            f'2>/dev/null T=$(: "\'"; echo {_V}); {_N} $T',
            f'1>&2 T=$(: "\'"; echo {_V}); {_N} $T',
            f'&>/dev/null T=$(: "\'"; echo {_V}); {_N} $T',
            f'{{fd}}>/dev/null T=$(: "\'"; echo {_V}); {_N} $T',
            # A redirection GLUED onto the program word is the program, not a leading
            # redirect: `kirocrew>/dev/null "$(printf token)"` runs the CLI with stdout
            # redirected and still mints, so it is refused. The prefix classifier
            # (`_push_token_redirection`) matches a redirect only after an fd number,
            # `&` or `{name}`, so `kirocrew` before the `>` keeps program position and
            # its basename resolves to the CLI. A fd-dup (`>&2`) and a file target are
            # the same.
            f'{_N}>/dev/null "$(printf {_V})"',
            f'{_N}>&2 "$(printf {_V})"',
            f'{_N}>/tmp/out "$(printf {_V})"',
            # A wrapper or command keyword keeps the next word in command position, so
            # `kirocrew` behind it is still the program and the quoted-substitution mint
            # is refused. `_program_positions` steps over wrappers and keywords through
            # the shared `_program_index`, so it agrees with `_is_self_program`.
            f'env {_N} "$(printf {_V})"',
            f'sudo {_N} "$(printf {_V})"',
            f'command {_N} "$(printf {_V})"',
            f'exec {_N} "$(printf {_V})"',
            f'nohup {_N} "$(printf {_V})"',
            f'time {_N} "$(printf {_V})"',
            f'timeout 5 {_N} "$(printf {_V})"',
            f'! {_N} "$(printf {_V})"',
            f'if {_N} "$(printf {_V})"; then :; fi',
            # `sudo --edit`/`-e` (sudoedit) runs no program, so on its own it is not a
            # mint -- but the floor lowercases before this walk and cannot tell `-e` from
            # `-E` (`--preserve-env`, which DOES run the command), so the sudoedit
            # no-program rule is NOT applied here and all four refuse. Refusing a
            # sudoedit form nobody mints with is the fail-closed side; applying the rule
            # on lowered text would let `sudo -E kirocrew "$(...)"` through.
            f'sudo --edit {_N} "$(printf {_V})"',
            f'sudo -e {_N} "$(printf {_V})"',
            f'sudo -E {_N} "$(printf {_V})"',
            f'sudo --preserve-env=PATH {_N} "$(printf {_V})"',
            # The r22 `+=` case. A verb reaches kirocrew assembled across an append.
            # These three are what the BASELINE already refuses, so they stay refused
            # with no alternative-pass `+=` modelling at all: `P=tok; P+=en` concatenates
            # two known literals into the verb, a bare `P+=token` appends to an empty
            # base, and `P=; P+=token` does the same after an empty assignment records
            # nothing. (The r22/r23 rows that ONLY the dropped fail-closed append and the
            # hand-copied subcommand tree refused -- the empty-reset, the opaque `+=`
            # tails and the subcommand-slot family -- are allowed on main and return to
            # that verdict; see ``test_an_opaque_append_rides_along_as_main_allows``.)
            f"P=tok; P+=en; {_N} $P",
            f"P+={_V}; {_N} $P",
            f"P=; P+={_V}; {_N} $P",
            # r26: a substitution assigned through a declaration builtin sets the variable
            # in THIS shell, so the mint is reached; the prefix test steps over the
            # declaration builtins (which `_program_positions` otherwise marks as argv[0]).
            f'export T=$(: "\'"; echo {_V}); {_N} $T',
            f'declare T=$(: "\'"; echo {_V}); {_N} $T',
            f'typeset T=$(: "\'"; echo {_V}); {_N} $T',
            f'local T=$(: "\'"; echo {_V}); {_N} $T',
            f'readonly T=$(: "\'"; echo {_V}); {_N} $T',
            # A redirect followed by a pipe or a `;` still ends the argv, so the mint in
            # the command after it is caught: a real pipe into the CLI, and a redirect
            # with a glued separator. The leading clobber `2>| /dev/null kirocrew ...`
            # itself is a documented residual (allowed, as on main) -- see MAIN_RESIDUALS.
            f'a | {_N} "$(printf {_V})"',
            f'echo 2>/dev/null; sudo {_N} "$(printf {_V})"',
            f'echo 2>/dev/null;sudo {_N} "$(printf {_V})"',
            f'echo >x| {_N} "$(printf {_V})"',
            # A `case`/`in`/`esac` word as an ARGUMENT, or a `$(case …)` that closes before
            # the mint, does not hide the later top-level mint: the raw depth walk returns
            # to top level once the substitution/subshell closes, so the mint is read.
            f'X=$(case x in (x) :;; esac); T=$(: "\'"; echo {_V}); {_N} $T',
            f'(case x in (x) :;; esac); T=$(: "\'"; echo {_V}); {_N} $T',
            f'X=$(echo case in x); T=$(: "\'"; echo {_V}); {_N} $T',
            f'(echo case in x); T=$(: "\'"; echo {_V}); {_N} $T',
        ],
    )
    def test_the_mint_is_refused(self, command: str) -> None:
        from kiro_crew.security import argv_floor_self

        assert argv_floor_self._is_credential_mint(command.lower()), command
        assert security.is_denied(command) is not None, command

    # Refused on purpose (fail closed): main allows each, because bash discards the
    # output that names the verb.  Deciding that means computing what the substitution
    # prints, which this floor does not do.
    FAIL_CLOSED = (
        [
            (f'python -m kiro_crew "$(echo {_V} {r}; echo doctor)"', f"{_NOT_READ} (`{r}`)")
            for r in _SENT_AWAY
        ]
        + [
            (f'T=$(: "\'"; echo {_V} {r}; echo doctor); {_N} $T', f"{_NOT_READ} (`{r}`)")
            for r in _SENT_AWAY
        ]
        + [
            (
                f'T=$(echo "it\'s" >/dev/null; echo {_V} >/dev/null; echo doctor); {_N} $T',
                f"{_NOT_READ} (`>/dev/null`)",
            ),
            (
                f'T=$(echo "it\'s" >/dev/null; : printf {_V}; echo doctor); {_N} $T',
                f"{_NOT_READ} (argument of `:`)",
            ),
            (
                f"T=$(echo \"it's\" >/dev/null; printf 'Explain '; echo {_V}); "
                f'{_N} chat -m "$T"',
                f"{_NOT_READ} (message text)",
            ),
            (
                f'python -m kiro_crew "$(echo {_V} 3>&1 1>/dev/null; echo doctor)"',
                f"{_NOT_READ} (`1>/dev/null`)",
            ),
            (
                f'python -m kiro_crew "$(echo {_V} 2>&1 1>&- ; echo doctor)"',
                f"{_NOT_READ} (`1>&-`)",
            ),
            (
                f'python -m kiro_crew "$(echo {_V} {{v}}>&1 >/dev/null; echo doctor)"',
                f"{_NOT_READ} (`>/dev/null`)",
            ),
            (
                f'python -m kiro_crew "$(echo {_V}>/dev/null; echo doctor)"',
                f"{_NOT_READ} (glued `>/dev/null`)",
            ),
            (
                f'python -m kiro_crew "$(echo {_V} | cat >/dev/null; echo doctor)"',
                f"{_NOT_READ} (pipe to a sink)",
            ),
            (
                f'python -m kiro_crew "$(echo {_V} | echo doctor)"',
                f"{_NOT_READ} (pipe into a printer)",
            ),
            (
                f"{_N} \"$(printf doctor; echo 'ignore; {_V} ; text' >&2)\"",
                f"{_NOT_READ} (`>&2`)",
            ),
            (f'{_N} "$(printf doctor # {_V}\n)"', f"{_NOT_READ} (comment)"),
            (
                f'python -m kiro_crew "$(echo checking {_V} >&2; echo doctor)"',
                f"{_NOT_READ} (`>&2`)",
            ),
        ]
    )

    @pytest.mark.parametrize("command, reason", FAIL_CLOSED)
    def test_a_verb_inside_a_substitution_is_refused_whatever_bash_does_with_it(
        self, command: str, reason: str
    ) -> None:
        assert len(self.FAIL_CLOSED) == 32
        assert security.is_denied(command) is not None, f"{reason}: {command}"

    def test_the_refusal_names_the_way_out(self) -> None:
        reason = security.is_denied(f'{_N} "$(printf {_V})"') or ""
        assert "write the verb as a plain word outside $( ) or backticks" in reason
        # And it names the variable escape for the assignment form (``T=$(...); $T``).
        assert "move the mint out of the substituted variable" in reason

    # Allowed, as on main: no mint verb word appears anywhere in the body, so there is
    # nothing for the floor to bind, and what the body computes is not read.
    NO_VERB_WORD = "no mint verb word appears anywhere in the body"
    RETURNING_ALLOWS = [
        'python -m kiro_crew "$(printf %s%s tok en)"',
        'T=$(: "\'"; printf %s%s tok en); kirocrew $T',
        'python -m kiro_crew "$(<<<x grep -o .)"',
        'python -m kiro_crew "$(printf tok; echo en | cat)"',
        'python -m kiro_crew "$(printf tok; cat <<<en)"',
        'python -m kiro_crew "$(echo tokem | tr m n)"',
        'python -m kiro_crew "$(cat)"',
        f"{_DIGEST}; {_N} $I",
        f"{_DIGEST}; $H $I",
        f"{_DIGEST}; sudo -u x $H $I",
    ]

    @pytest.mark.parametrize("command", RETURNING_ALLOWS)
    def test_a_body_naming_no_verb_is_allowed(self, command: str) -> None:
        assert len(self.RETURNING_ALLOWS) == 10
        assert security.is_denied(command) is None, f"{self.NO_VERB_WORD}: {command}"

    @pytest.mark.parametrize(
        "command",
        [
            f"T=$(date); {_N} $T",
            f"T=$(date); echo {_V}; {_N} $T",
            f'{_N} doctor "$(date)"',
            'T=$(: "\'"; echo hi); echo $T',
            f'T=$(: "\'"; echo {_V}); T={_N}; $T doctor',
            f"{_N} chat -m \"$(printf 'Explain /api {_V} authentication')\"",
            f"python -m kiro_crew chat -m \"$(printf 'a {_V} b')\"",
            f"python -m kiro_crew > /tmp/out chat -m \"$(printf 'a {_V} b')\"",
            f'{_N} > "$(printf {_V})" doctor',
            f'{_N} >"$(printf {_V})" doctor',
            f'{_N} >/dev/null doctor "$(date)"',
            "; ".join(f"A{i}=$(printf {_N})" for i in range(9)) + f'; T=$(printf {_V}); echo "$T"',
            "; ".join(f"A{i}=$({_N} status {i})" for i in range(9)) + '; echo "$A0"',
            f'{_DIGEST}; echo "$A $B $C $D $E $F $G $H $I"',
            f'{_N} "$(printf %s doctor)"',
            'python -X dev -m kiro_crew "$(printf doctor)"',
            "python -X \"$(printf 'int_max_str_digits=%d' 640)\" -m kiro_crew doctor",
            'python -X"$(printf %d 1)" -m kiro_crew doctor',
            'python -m kiro_crew "$(printf -- --version)"',
            # A redirect glued to a command separator still ends the argv, so a verb in
            # the next command is not the subcommand (``>|``/``{{fd}}>`` keep their exemption).
            f"{_N} logs 2>/dev/null|grep -i {_V}",
            f"{_N} doctor 2>&1; echo {_V}",
            f"{_N} doctor 2>/dev/null; gh auth {_V}",
            f"{_N} --help >/dev/null; export GH_TOKEN=$(gh auth {_V})",
            # A heredoc'd comment line opens with ``*``, a word of glob characters only:
            # prose, not a program, so its quoted text is not read as a subcommand.
            f'* "$(printf {_V})"',
            f"cat > /tmp/theme.ts <<'EOF'\n/**\n * `{_V}: [light, dark]`.\n */\nEOF",
            f" * (`?{_V}=x`), which must never be written to a log line.",
            # The verb inside a path is part of that word, not a word of its own.
            f'SPEC=$(cat docs/system-specs/modules/dashboard-{_V}-auth.md); {_N} chat -m "$SPEC"',
            f'S=$(cat src/{_V}.py); {_N} chat -m "$S"',
            # A program binding reads only its own substitution, not a later one's verb.
            f"K=$(which {_N}); T=$(gh auth {_V}); $K status; echo $T",
            f'P=$(which {_N}); T=$(gh auth {_V}); $P chat -m "hello $T"',
            # An apostrophe leaves the substitution's boundary unreadable, but here the
            # verb sits glued inside a quoted phrase ("... token docs"), so it is not a
            # standalone verb word; the first mint-relevant name the fail-closed scan
            # meets is the program, bound harmlessly, and main allows it too.  (Its
            # siblings where the verb IS a standalone later word are pinned refused --
            # see the fail-closed apostrophe test.)
            f'T=$(echo "it\'s ok"); echo "see dashboard {_V} docs"; {_N} chat -m "$T"',
            # The verb sits after a quoted ``)`` that closes the body early, or in a
            # later command's prose: a residual main leaves open too (see "Not a goal").
            # Both the floor form below and its assignment twin
            # ``T=$(: ")"; printf <verb>); <name> $T`` keep the dequoted single-token
            # read: the quotes are balanced, so the reader ends the body at the quoted
            # ``)`` and never sees the verb, exactly as on main.  Pinning them would
            # need the raw-source boundary read a prior round could not make robust, so
            # they are accepted as documented residuals (see the PR body).
            f'{_N} "$(: ")"; printf {_V})"',
            f'P=$(which {_N}); $P chat -m "1) rotate the {_V} 2) restart"',
            f'V=$(cat VERSION); case $1 in {_V}) {_N} doctor "$V";; esac',
            # A verb-bound variable splits only the tokens that use it.
            f'export GH_TOKEN=$(gh auth {_V}); git commit -m "fix: {_N} restart reloads {_V}"',
            f'export GH_TOKEN=$(gh auth {_V}); gh pr create --body "{_N} restart after {_V} change"',
            f'export GH_TOKEN=$(gh auth {_V}); git commit -m "fix {_V} refresh; kill stale {_N}"',
        ],
    )
    def test_main_allowed_spellings_stay_allowed(self, command: str) -> None:
        assert security.is_denied(command) is None, command

    # Allowed, as on main (documented residuals, listed in the PR body).  Under the
    # body-scoped "protected program wins" rule, a substitution whose BODY (bounded by
    # the paren count) names neither a protected program nor the verb binds nothing, so
    # the command is left exactly as main leaves it.  That covers: a verb that is only
    # in a LATER command after the real ``)`` (``git log --format="%an's"); ... ;
    # kirocrew chat -m "$T"`` -- bash runs ``kirocrew "<log>"``, no mint); a here-string,
    # inner assignment or glued phrase the floor does not evaluate; and a BALANCED body
    # that names no verb or program (``echo done``), where a later apostrophe is
    # irrelevant because the alternative reads only the substitution's own body.  The
    # quoted-``)`` and backtick-body spellings are NOT here: their bodies DO name the
    # verb (bash runs the mint), and the raw-body reader now refuses them.
    MAIN_RESIDUALS = [
        f'python -m kiro_crew "$(cat <<<{_V})"',
        f'python -m kiro_crew "$(X={_V}; echo $X)"',
        # A DIGIT glued to the program word (`kirocrew2>/dev/null`) is part of the word:
        # bash runs `kirocrew2`, not the CLI, so it is not a mint and stays allowed, as on
        # main. Only `>`, `&>`, an fd-then-operator, or `{name}>` is a redirection here.
        f'{_N}2>/dev/null "$(printf {_V})"',
        # `coproc NAME cmd` with a SIMPLE command: bash reads the first word after
        # `coproc` as the program, not a coprocess name (a name needs a compound command,
        # verified on bash 5.2), so `coproc worker kirocrew "$(...)"` runs `worker`, not
        # the CLI, and is not a mint -- the no-name `coproc kirocrew "$(...)"` runs the CLI
        # and is refused by the quoted-substitution reading.
        f'coproc worker {_N} "$(printf {_V})"',
        f'T=$(echo "it\'s {_V}"); {_N} chat -m "$T"',
        f'T=$(echo "it\'s {_V}" 2>/dev/null); {_N} chat -m "$T"',
        f'T=$(echo "it\'s ok"); grep {_V} f; {_N} $T',
        f'T=$(git log --format="%an\'s"); git grep {_V}; {_N} chat -m "$T"',
        f'T=`echo done`; {_N} $T; echo "it\'s"',
        f'T=$(echo done); {_N} $T; echo "it\'s"',
        # A later assignment that names nothing replaces the earlier verb, as in bash
        # (gpt r25): each assignment takes its own reading, not one per name.
        f'T=$(: "\'"; echo {_V}); echo $T; T=$(echo doctor); {_N} $T',
        f'T=$(: "\'"; echo {_V}); T=$(echo status); {_N} $T',
        # A subshell's or a quoted string's ``T=$(...)`` sets nothing in this shell, so
        # its verb never reaches the later ``T`` (gpt r26).
        f"(T=$(echo {_V})); T=$(echo doctor); {_N} $T",
        f'echo " T=$(: \\"\'\\"; echo {_V})"; T=$(echo doctor); {_N} $T',
        # The alternative binds only an assignment that sets a variable in THIS shell
        # (gpt r19): not one in a subshell, an argument such as ``echo T=$(...)``, a
        # nested substitution or an uncalled function body.  Same verdict as main.
        f'T=doctor; (true; T=$(: "\'"; echo {_V})); {_N} $T',
        f'echo T=$(: "\'"; echo {_V}); {_N} $T',
        f'X=$(T=$(: "\'"; echo {_V})); {_N} $T',
        f'f() {{ T=$(: "\'"; echo {_V}); }}; {_N} $T',
        # A function body whose ``{`` is glued to ``f()`` is still not this shell's
        # scope (gpt r19b): an uncalled function never sets ``T``, so the later use reads
        # ``doctor``.  A bare ``{ ...; }`` group IS this shell, so it binds ``T`` to the
        # mint -- but the later ``T=doctor`` overwrites that, so the final ``$T`` is
        # ``doctor`` and the command stays allowed, as on main (r26).
        f'T=doctor; f(){{ T=$(: "\'"; echo {_V}); }}; {_N} $T',
        f'{{ T=$(: "\'"; echo {_V}); }} >/dev/null; T=doctor; {_N} $T',
        # A leading clobber redirect (`2>| /dev/null kirocrew "$(...)"`) is a documented
        # residual: the program-position walk reads dequoted tokens and cannot tell the
        # clobber `>|` from a quoted `>` beside a pipe, so this is left as main leaves it
        # (allowed). See the follow-up issue; the quote-aware fix belongs in its own PR.
        f'2>| /dev/null {_N} "$(printf {_V})"',
        # A bare glob word in the body is a filename pattern, not the program (opus r19).
        f"F=$(ls test/*); pytest $F -k {_V}",
        # Security Scope Review (r20 F3): the product name as an OPERAND of another
        # program never arms the second reading -- a repo slug, a secret/service/
        # container name, or a glob-bound variable used as pytest's argument. The
        # program word is gh/kubectl/security/docker/pytest, and its own
        # ``$(gh auth token)`` is that tool's business, so main's allow stands. The
        # pytest row's ``$TESTS`` binds a program name through a glob but is USED as an
        # argument, so the alternative leaves it unresolved.
        f'gh secret set GH_PAT --repo kirodotdev/KiroCrew --body "$(gh auth {_V})"',
        f'gh secret set GH_PAT -R kirodotdev/KiroCrew --body="$(gh auth {_V})"',
        f'kubectl create secret generic {_N} --from-literal=GH_TOKEN="$(gh auth {_V})"',
        f'security add-generic-password -a "$USER" -s {_N} -U -w "$(gh auth {_V})"',
        f"TESTS=$(git ls-files 'test/*crew*'); pytest $TESTS -k {_V}",
        f'docker run --name {_N} -e GH_TOKEN="$(gh auth {_V})" image',
        # The product name as pytest's operand, with the substitution as another of its
        # operands: argv[0] is pytest, so the quoted-substitution reading does not arm
        # (contrast the argv[0] form ``kirocrew "$(printf token)"``, pinned refused).
        f'pytest {_N} -k "$(printf {_V})"',
        # A glob-bound program variable used as another program's operand keeps main's
        # allow through EVERY reference spelling, not just a plain ``$VAR`` (gpt r20 F3):
        # a transform or an indirect reference to it is still an operand, so the
        # program-position gate leaves it unresolved.
        f"TESTS=$(git ls-files 'test/*crew*'); pytest ${{TESTS:0}} -k {_V}",
        f"TESTS=$(git ls-files 'test/*crew*'); pytest ${{TESTS^^}} -k {_V}",
        f"TESTS=$(git ls-files 'test/*crew*'); W=TESTS; pytest ${{!W}} -k {_V}",
        # An ALIAS of a glob-bound program variable carries the same program-only
        # provenance, so it too stays an operand of another program (gpt r20 F3b):
        # ``U=$TESTS`` and a chain ``V=$U`` are both left literal at pytest's argument.
        f"TESTS=$(git ls-files 'test/*crew*'); U=$TESTS; pytest $U -k {_V}",
        f"TESTS=$(git ls-files 'test/*crew*'); U=$TESTS; V=$U; pytest $V -k {_V}",
        f"TESTS=$(git ls-files 'test/*crew*'); U=$TESTS; W=U; pytest ${{!W}} -k {_V}",
        f"TESTS=$(git ls-files 'test/*crew*'); U=; U+=$TESTS; pytest $U -k {_V}",
        # r25 (gpt): an append that KEEPS the glob-bound program variable's binding
        # (an opaque `+=$(true)` tail, or an empty `+=''`) must keep its program-only
        # provenance -- $TESTS stays pytest's operand, not a mint. Clearing the flag on
        # such an append falsely refused these; they are allowed on main and here.
        f"TESTS=$(git ls-files 'test/*crew*'); TESTS+=$(true); pytest $TESTS -k {_V}",
        f"TESTS=$(git ls-files 'test/*crew*'); TESTS+=''; pytest $TESTS -k {_V}",
        # The alias RHS itself may be a transform or indirect reference -- still an operand
        # use of the glob-bound program, so still allowed (gpt/opus r20 F3c).
        f"TESTS=$(git ls-files 'test/*crew*'); U=${{TESTS:0}}; pytest $U -k {_V}",
        f"TESTS=$(git ls-files 'test/*crew*'); W=TESTS; U=${{!W}}; pytest $U -k {_V}",
    ]

    # Main parity, NOT this PR's refusal: main's resolver binds the argument-shaped
    # ``t=$(echo token)`` token although bash never assigns it, so main refuses this
    # command and the union (which keeps main's frame) does too.  It is declared in the
    # PR body as a residual of main's over-binding.
    def test_an_argument_shaped_assignment_keeps_mains_refusal(self) -> None:
        from kiro_crew.security import shell_normalizer as _sn

        command = f"echo T=$(echo {_V}); {_N} $T"
        assert _sn._self_tokens(command.lower(), alternative=True) == []
        assert security.is_denied(command) is not None

    def test_a_rebinding_back_to_the_program_name_is_allowed_because_it_is_not_the_verb(
        self,
    ) -> None:
        # The reverse of the gpt r21 F1 trigger: a variable bound to the verb and then
        # REBOUND to the program name ends holding the program name, so `kirocrew $P`
        # resolves to `kirocrew kirocrew` -- no `token` verb, not a mint -- and main
        # allows it too. Provenance re-derives from the latest binding (the program),
        # so $P is program-only and stays literal at the operand; the verdict is allow
        # on both main and head, NOT a refusal.
        command = f'P=$(: "\'"; echo {_V}); P=$(echo {_N}); {_N} $P'
        assert security.is_denied(command) is None, command

    def test_a_plusequals_that_assembles_the_program_name_then_mints_is_refused(
        self,
    ) -> None:
        # The r22 `+=` program-assembly case. `+=` concatenates two known literals, so
        # `P=kiro; P+=crew` builds the program name and `$P token` is `kirocrew token` --
        # a mint. Refused on both main and head (main already concatenates known
        # literals); pinned here with the derived verdict (REFUSE) because the verb is
        # not in the diff's new path -- the `+=` assembles the PROGRAM, and it is the
        # trailing `token` operand that makes the assembled program a mint.
        command = f"P={_N[:4]}; P+={_N[4:]}; $P {_V}"
        assert security.is_denied(command) is not None, command

    def test_a_plusequals_that_does_not_assemble_the_verb_stays_allowed(self) -> None:
        # The counterpart to the `+=` refusals: the empty-assignment reset and the
        # concatenation model must not OVER-refuse. An append on an unrelated variable,
        # an append that builds a non-verb operand, and an empty reassignment followed
        # by a non-verb append all stay allowed, matching bash and main.
        for command in (
            f"A=x; A+=y; {_N} doctor",  # unrelated variable, not fed to the mint
            f"A=foo; A+=bar; {_N} $A",  # operand built to "foobar", not the verb
            # No empty-reset modelling now: $P reads as "kirocrewdoctor" (not the verb),
            # so this stays allowed, matching bash and main.
            f"P=$(echo {_N}); P=; P+=doctor; {_N} $P",
            "Q=; Q+=x; echo $Q",  # empty-then-append on a variable never fed to the mint
        ):
            assert security.is_denied(command) is None, command

    def test_an_opaque_append_rides_along_as_main_allows(self) -> None:
        # An opaque / unknown-base `+=` (``P+=$($Q)``, ``P+=$(true)``, ``OPTS+=x``, an
        # empty-reset ``P=; P+=token``, an unresolvable tail) has no literal to
        # concatenate, so neither main nor the alternative pass isolates the verb: the
        # command carries main's verdict -- allowed -- in EVERY position, behind a
        # wrapper, a redirect or a top-level flag, under ``pod`` / ``pod exec``, through
        # the module, a nested shell, a quoted or braced use, or an alias. The mint this
        # floor guards is one whose SUBCOMMAND comes from a substitution, not one
        # assembled across an opaque append, so every row here is allowed on main and the
        # vs-main oracle stays 0 refused-on-main->allowed.
        prefix = f"X=$(echo {_N}); P=; P+=$($Q); "
        for use in (
            f"{_N} --json $P",
            f'{_N} "$P"',
            f'{_N} "${{P}}"',
            f"{_N} -v $P",
            f"{_N} 2>/dev/null $P",
            f"{_N} > /tmp/o $P",
            f">/tmp/o {_N} $P",
            f"{_N} pod $P",
            f"{_N} pod exec w $P",
            f"{_N} pod exec w -- $P",
            f"{_N} pod $(true) exec w $P",
            f"{_N} pod $E exec w $P",
            f"{_N} pod exec w $(true) pod $(true) exec v $P",
            f"{_N} pod $(echo exec) status $P",
            f"{_N} pod $W status $P",
            f"{_N} $Y status $P",
            f"{_N} -$(echo v exec) w $P",
            f"env {_N} $P",
            f"command {_N} $P",
            f"exec {_N} $P",
            f"sudo {_N} $P",
            f"timeout 5 {_N} $P",
            "python -m kiro_crew $P",
            f'bash -c "{_N} $P"',
            f"U=$P; V=$U; {_N} $V",
        ):
            assert security.is_denied(prefix + use) is None, use
        for command in (
            # Security Scope Review rows: an opaque `+=` feeding a non-mint subcommand
            # argument (a chat message, a status flag), allowed as on main.
            "REPO=$(git rev-parse --show-toplevel); SUMMARY=; "
            f'SUMMARY+=$(git -C "$REPO" log --oneline -3); {_N} chat -m "$SUMMARY"',
            f'VER=$({_N} --version); MSG="Upgraded to "; MSG+=$(git describe --tags); '
            f'{_N} chat -m "$MSG"',
            f"VER=$({_N} --version); OPTS+=--json; {_N} status $OPTS",
            f"VER=$({_N} --version); OPTS+=--json; X=$OPTS; {_N} status $X",
            f"VER=$({_N} --version); OPTS+=--json; {_N} pod exec w status $OPTS",
            # Unknown-base `+=` (unset OPTS) and the r22 empty-reset / unresolvable-tail
            # rows: no literal to concatenate, so the verb is never isolated.
            f"X=$(echo {_N}); OPTS+=x; {_N} $OPTS",
            f"P=$(echo {_N}); P=; P+={_V}; {_N} $P",
            f'P=$(echo {_N}); P=; P+=$(: "\'"; echo {_V}); {_N} $P',
            f"X=$(echo {_N}); P=; P+=$(true); {_N} $P",
        ):
            assert security.is_denied(command) is None, command

    def test_a_wrapper_with_a_harmless_operand_stays_allowed(self) -> None:
        # The wrapper widening is ONLY the quoted-substitution mint shape: a wrapper or
        # keyword in front of the CLI running a non-mint subcommand keeps main's allow.
        for command in (
            f"env {_N} status",
            f"sudo {_N} doctor",
            f"command {_N} --help",
            f"exec {_N} status",
            f"nohup {_N} ls",
            f"time {_N} status",
            f"timeout 5 {_N} status",
            f"! {_N} status",
            f"if {_N} status; then :; fi",
        ):
            assert security.is_denied(command) is None, command

    @pytest.mark.parametrize("command", MAIN_RESIDUALS)
    def test_main_residuals_stay_allowed(self, command: str) -> None:
        assert security.is_denied(command) is None, command

    # GPT's r21 block on the superseded head: the old whole-assignment-token scan bound
    # ``kirocrew`` from the ``${KIROCREW:+...}`` value PREFIX.  The alternative now reads
    # only the substitution's OWN body, so a name in the prefix is never bound -- the
    # head adds no refusal here.  This is MAIN PARITY: the command stays refused, but
    # through MAIN's own resolution, whose reader over-reads the lone quote to the prefix
    # name.  That over-read is pre-existing and kept untouched (the union never un-refuses
    # main, and this round changes no main reader); it is a documented over-read residual
    # in the PR body.
    def test_the_alternative_reads_the_body_not_the_value_prefix(self) -> None:
        from kiro_crew.security import shell_normalizer as _sn

        src = f'T=${{KIROCREW:+"\'"}}$(printf doctor); $T {_V}'.lower()
        assert (
            _sn._self_tokens(src, alternative=True) == []
        ), "the alternative must not bind from the prefix"
        # Main parity: the remaining refusal is main's own over-read, which the union keeps.
        assert security.is_denied(f'T=${{KIROCREW:+"\'"}}$(printf doctor); $T {_V}') is not None

    # The alternative reads ONE body per assignment, so a command of many
    # substitution-valued assignments costs it about as much as each assignment's own
    # body, not the whole rest of the line again per assignment. Counted in interpreter
    # instructions rather than seconds (``_interpreter_work``), so it holds on any runner.
    def test_the_alternative_reads_each_body_once(self) -> None:
        from denied_commands_security_helpers import _interpreter_work, _linear_work

        from kiro_crew.security import shell_normalizer as _sn

        def build(n: int) -> str:
            return "; ".join(f"a{i}=$(echo x)" for i in range(n)) + f"; {_N} status"

        work = [
            _interpreter_work(lambda n=n: _sn._self_tokens(build(n), alternative=True))
            for n in (100, 200, 300)
        ]
        assert _linear_work(work), work

    # r24 budget pin. The dropped `+=` fail-close recomputed, for EVERY `$`-token, a scan
    # back over the whole resolved prefix (``_after_a_non_mint_subcommand(out, ...)``) --
    # O(n^2) in the token count, ~n^2/2 predicate calls near the 8 KiB classified cap
    # (``MAX_CLASSIFIED_COMMAND_CHARS``), which stalls the synchronous PreToolUse gate.
    # The resolver is now O(1) per `$`-use: ``at_program`` is one lookup into the
    # precomputed ``program_positions`` and ``_resolve_name`` closes over
    # ``alt_program_only`` alone -- no per-token rescan of ``out``. Pinned on the exact
    # shape that drove the rescan (an alternative-tracked program-only variable used as an
    # operand many times over) at sizes whose command text runs past the classified cap
    # (each use is ~22 bytes, so 300/600/900 uses is ~6.6/13/20 KiB).
    # ``test_the_alternative_reads_each_body_once`` pins the per-assignment-body axis;
    # this pins the per-use axis that one missed.
    def test_the_operand_resolution_is_linear_per_use(self) -> None:
        from denied_commands_security_helpers import _interpreter_work, _linear_work

        from kiro_crew.security import shell_normalizer as _sn

        def build(n: int) -> str:
            return "TESTS=$(git ls-files 'test/*crew*'); " + "; ".join(
                f"pytest $TESTS -k {_V}" for _ in range(n)
            )

        work = [
            _interpreter_work(lambda n=n: _sn._self_tokens(build(n), alternative=True))
            for n in (300, 600, 900)
        ]
        assert _linear_work(work), work

    # Opus r26i / GPT r28: a cost shortcut that returned None for an UNPROVEN substitution
    # body was tried and REVERTED -- it was a fail-open. `_raw_substitution_mint_name`
    # keeps a460e18d's whole-remainder read. These three bodies are unprovable to the
    # closer walk but bash DOES close them (a `#` comment whose terminating newline folds
    # to `;`; a `${x:-(}` paren; a heredoc `(`), and each names a self program/verb at the
    # use site -- a real mint. The baseline reader does NOT backstop the lettered-glob
    # self-PROGRAM spelling (`kirocre[w]`); only `_raw_substitution_mint_name` reads it, so
    # the refusal lives solely here. Three-tree (verified): each row is allowed on
    # origin/main, REFUSED on a460e18d (this head), and was allowed on the reverted
    # None-for-unproven shortcut. This head must REFUSE all three.
    def test_a_bash_closable_unprovable_body_mint_stays_refused(self) -> None:
        glob = _N[:7] + "[" + _N[7] + "]"  # kirocre[w]: a lettered-glob self spelling
        reproducers = [
            f"F=$(: # )\nprintf {_N}); $F {_V}",  # folded-newline literal (gpt)
            f"T=$(: ${{x:-(}}; printf {glob}); $T {_V}",  # param-default paren (opus)
            f"T=$(cat <<E\n(\nE\nprintf {glob}); $T {_V}",  # heredoc paren (opus)
        ]
        for cmd in reproducers:
            assert security.is_denied(cmd), f"must refuse (bash closes this mint): {cmd!r}"

    # The never-closing trigger's cost is main's BASELINE reader (O(n^3), pre-existing, a
    # documented main-side DoS residual). This pins that the ALTERNATIVE pass this PR adds
    # is NOT the dominant cost -- the stall is main's, not this PR's. A relative share,
    # never an absolute wall-clock (which would be main's number and runner-dependent).
    # The fail-closed reading pairs every never-closing assignment, so the alternative
    # pass is ~29% of the gate at 1 KiB and stays FLAT across sizes (29% / 31% / 31% at
    # 1 / 2 / 4 KiB): the same order as the baseline, so the bound is "below half the gate".
    def test_the_alternative_pass_is_a_small_share_of_the_gate(self) -> None:
        import time

        from kiro_crew.security import shell_normalizer as _sn

        cmd = "a=$(: #); " * 100 + f"; {_N} doctor"  # ~1 KiB: share is flat, so this
        # proxies 2/4 KiB while keeping the test well under the per-test timeout.
        spent = 0.0
        real = _sn._alternative_mint_overrides

        def timed(*a, **k):
            nonlocal spent
            t0 = time.perf_counter()
            try:
                return real(*a, **k)
            finally:
                spent += time.perf_counter() - t0

        with pytest.MonkeyPatch.context() as scoped:
            scoped.setattr(_sn, "_alternative_mint_overrides", timed)
            t0 = time.perf_counter()
            security.is_denied(cmd)
            total = time.perf_counter() - t0
        share = spent / total
        assert share < 0.50, (
            f"alternative pass is {share:.1%} of the gate ({spent:.3f}s of {total:.3f}s) "
            "on the ~1 KiB never-closing trigger -- over the 50% bound; main's baseline "
            "reader (O(n^3), a documented main-side DoS residual) is the dominant cost, "
            "not this PR's alternative pass (~29% at 1 KiB, ~31% at 2 KiB, flat across sizes)"
        )

    # GPT r28 F1: a program-only alternative binding ($F bound to a glob / ``./`` path self
    # program read from a substitution body) was DROPPED when the alternative frame's nested
    # ``bash -c "$F token"`` payload was walked -- the ``-c`` script ``$F token`` descended
    # without the binding, so ``is_denied`` returned None while bash ran ``./kirocrew token``
    # and the local-token endpoint minted an owner credential. The fix carries the frame's
    # bindings into the nested walk and resolves them at the INNER program positions; a data
    # operand (``bash -c "echo $F"``) stays unexpanded. Three-tree (verified): each glob mint
    # row is allowed on origin/main AND on the pushed head 571d9e9e07f4, and REFUSED here; the
    # plain ``./kirocrew`` spelling is refused on all three (main's own reader binds it); the
    # data-operand control is allowed on all three.
    def test_a_program_only_binding_is_carried_into_nested_payloads(self) -> None:
        from kiro_crew.security import shell_normalizer as _sn

        glob = _N[:7] + "[" + _N[7] + "]"  # kirocre[w]
        # Glob-spelled rows: allowed on the pushed head, refused only by this fix.
        glob_mints = [
            f'F=$(: "\'"; printf %s ./{glob}); bash -c "$F {_V}"',
            f'F=$(: "\'"; printf %s ./{glob}); sh -c "$F {_V}"',
            f'F=$(: "\'"; printf %s ./{glob}); env bash -c "$F {_V}"',
            f'F=$(: "\'"; printf %s ./{glob}); sudo bash -c "$F {_V}"',
            f'F=$(: "\'"; printf %s ./{glob}); timeout 5 bash -c "$F {_V}"',
        ]
        for cmd in glob_mints:
            assert security.is_denied(cmd), f"bash runs ./{_N} {_V}: {cmd!r}"
        # The plain spelling is refused independently (main's reader binds ./kirocrew).
        plain = f'F=$(: "\'"; printf %s ./{_N}); bash -c "$F {_V}"'
        assert security.is_denied(plain), plain
        # A program-only glob used as a DATA operand is not a program; stays allowed,
        # exactly as the pushed head -- operands are not re-expanded.
        control = f'F=$(: "\'"; printf %s ./{glob}); bash -c "echo $F"'
        assert security.is_denied(control) is None, control

        # Mutant: drop the carried bindings (strip the seed kwargs). Each glob mint must
        # then go allowed -- the carried binding is what refuses them.
        real = _sn._resolve_local_assignments

        def _drop_seeds(
            tokens,
            *,
            mint_overrides=None,
            seed_values=None,
            seed_program_only=None,
            bindings_out=None,
        ):
            return real(tokens, mint_overrides=mint_overrides, bindings_out=bindings_out)

        with pytest.MonkeyPatch.context() as scoped:
            scoped.setattr(_sn, "_resolve_local_assignments", _drop_seeds)
            for cmd in glob_mints:
                assert security.is_denied(cmd) is None, f"mutant should drop refusal: {cmd!r}"

    # DOCUMENTED OVER-REFUSAL (fail-closed), accepted as a residual (conductor r29). The
    # program-only carry leaks the binding into its OWN initializer when the pre-existing
    # lone-quote tokenizer defect mis-surfaces the initializer's nested ``bash -c`` as a
    # top-level carrier. The command is non-executable -- ``$F`` is unset while its own RHS
    # runs -- so refusing is the safe (fail-closed) direction; main and the pushed head
    # 571d9e9e07f4 both ALLOW it. Pinned REFUSED so a later change that flips it is VISIBLE
    # rather than silent (if this starts to pass, the self-initializer carry was fixed --
    # update the "Residual (over-refusal, fail-closed)" row in the PR body).
    def test_the_self_initializer_carry_is_a_documented_over_refusal(self) -> None:
        glob = _N[:7] + "[" + _N[7] + "]"
        reproducer = f'F=$(: "\'"; printf %s ./{glob}; bash -c "$F {_V}"); true'
        assert security.is_denied(reproducer), (
            "documented fail-closed over-refusal; if this now passes, the self-initializer "
            "carry was fixed -- update the Not-covered residual in the PR body"
        )

    # Opus r29->r30: a bare `{` word or a finished `case` earlier on the line left the
    # running grouping depth open/negative, so `_raw_word_scopes` marked the real
    # top-level `T=` as nested, `_alternative_mint_overrides` skipped it (`and top_level`),
    # no alt frame was built, and the baseline over-read the lone quote -> `kirocrew token`
    # ALLOWED. `_raw_word_scopes` now fails CLOSED: an unbalanced line (depth left open, or
    # it went negative) treats every word as top level, so the alt frame is built and the
    # mint is refused. Balanced groups (an uncalled function body, a real `{ ...; }` group)
    # are unchanged. Three-tree (verified): each trigger allowed on main AND pushed
    # 4f04687c01df, REFUSED here; each control allowed on all three.
    def test_an_unbalanced_prefix_cannot_drop_the_alternative_frame(self) -> None:
        from kiro_crew.security import shell_normalizer as _sn

        tail = f'T=$(: "\'"; echo {_V}); {_N} $T'
        triggers = [
            f"echo {{; {tail}",  # bare `{` word (opus)
            f"case x in x) :;; esac; {tail}",  # finished case (opus)
            f"case x in (x) :;; esac); {tail}",  # a `)` after esac (broken)
        ]
        for cmd in triggers:
            assert security.is_denied(cmd), f"unbalanced prefix must not drop the frame: {cmd!r}"
        controls = [
            f'f(){{ T=$(: "\'"; echo {_V}); }}; {_N} $T',  # uncalled function body
            f'function f {{ T=$(: "\'"; echo {_V}); }}; {_N} $T',  # function keyword
            f'{{ T=$(: "\'"; echo {_V}); }}; {_N} $T',  # real brace group (residual)
        ]
        for cmd in controls:
            assert security.is_denied(cmd) is None, f"balanced group stays allowed: {cmd!r}"

        # Mutant: restore the pre-fix `_raw_word_scopes` (no fail-closed override). The
        # triggers must then go allowed -- the fail-closed classification is what refuses.
        def _pre_fix(command: str) -> "list[tuple[int, bool]]":
            words: "list[tuple[int, bool]]" = []
            depth = params = 0
            broken = in_word = False
            for step in _sn._iter_shell_chars(command):
                if step.active and step.char.isspace():
                    in_word = False
                    continue
                if not in_word:
                    in_word = True
                    words.append((step.offset, depth == 0 and not broken))
                if step.char == "{" and command[step.offset - 1 : step.offset] == "$":
                    params += 1
                elif step.char == "}" and params:
                    params -= 1
                elif step.active and step.char in "({":
                    depth += 1
                elif step.active and step.char in ")}":
                    depth -= 1
                broken = broken or depth < 0
            return words

        with pytest.MonkeyPatch.context() as scoped:
            scoped.setattr(_sn, "_raw_word_scopes", _pre_fix)
            for cmd in triggers:
                assert security.is_denied(cmd) is None, f"mutant should drop refusal: {cmd!r}"

    # ── Monotonicity, proven structurally ──
    # The union keeps main's resolution untouched and only APPENDS the alternative frame
    # (and descends its nested payloads); it never drops or rewrites a baseline frame.
    # The floor refuses when ANY frame is a mint, so the head refuses a SUPERSET of what
    # main refuses -- every command main refuses, this head refuses.  These two tests pin
    # that by walking each command with the alternative ON and OFF (OFF reproduces main,
    # whose resolver is this module unchanged) and asserting the OFF frames are a subset
    # of the ON frames: over a curated corpus, and over every substitution line in the
    # repo (the per-line grep the earlier rounds ran by hand).
    @staticmethod
    def _frames(command: str, *, alternative: bool) -> "set[tuple[str, tuple[str, ...]]]":
        from kiro_crew.security import shell_normalizer as _sn

        with pytest.MonkeyPatch.context() as scoped:
            if not alternative:
                scoped.setattr(_sn, "_alternative_mint_overrides", lambda *a, **k: {})
            walk = _sn._shell_payload_walk_uncached(command.lower(), True)
        return {(source, tuple(tokens)) for source, tokens in walk}

    @pytest.mark.parametrize(
        "command",
        [
            f'T=$(: "\'"; echo {_V}); {_N} $T',
            f'T=$(: "\'"; : {_V}; echo {_N}); $T {_V}',
            f'T=${{U:+"\'"}}$(printf {_V}); {_N} $T',
            f'T=$(: ")"; printf {_V}); {_N} $T',
            f'T=`: "\'"; echo {_V}`; {_N} $T',
            f'T=`printf %s {_V}`; {_N} $T; echo "it\'s done"',
            f'T=${{KIROCREW:+"\'"}}$(printf doctor); $T {_V}',
            f'T=$(: "\'"; echo {_V}); bash -c "{_N} $T"',
            f'T=$(echo "it\'s {_V}"); {_N} chat -m "$T"',
            f'T=$(echo done); {_N} $T; echo "it\'s"',
            f"echo hello; ls -la; {_N} status",
        ],
    )
    def test_the_union_only_adds_frames(self, command: str) -> None:
        assert self._frames(command, alternative=False) <= self._frames(
            command, alternative=True
        ), command

    #: Sharded so each instance stays well under the repo's 120s per-test timeout on
    #: slow CI runners (this test hit the shard-5 timeout on an earlier head at ~14.5s
    #: local, so the runner is ~8x slower here) and xdist can run the shards in parallel.
    #: The lines are pure in-process frame walks with no subprocess to batch, so the only
    #: lever is to split the corpus; every shard runs the identical per-line assertion and
    #: the shards partition the git-grep output with no overlap or gap.
    _UNION_LINE_SHARDS = 8

    @pytest.mark.parametrize("shard", range(_UNION_LINE_SHARDS))
    def test_the_union_adds_frames_on_every_repo_substitution_line(self, shard) -> None:
        import subprocess

        root = pathlib.Path(__file__).resolve().parents[1]
        try:
            out = subprocess.run(
                ["git", "grep", "-hI", "-E", r"(\$\(|`)[^`)]*\btoken\b"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=FALLBACK_TIMEOUT_SECS,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - env without git
            pytest.skip("git grep unavailable")
        lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        if not lines:
            pytest.skip("no substitution lines found")
        for line in lines[shard :: self._UNION_LINE_SHARDS]:
            base = self._frames(line, alternative=False)
            assert base <= self._frames(line, alternative=True), line
