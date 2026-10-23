"""Tests for kiro_crew.security: the shell grammar the self-protection gate reads.

Substitution bodies end where command grammar ends them, ``case`` arms only in command
position, and line continuations fold before tokenizing, inside substitution openers too.
"""

from __future__ import annotations

import pytest

from kiro_crew import security


class TestSubstitutionCloserReadsCommandGrammar:
    """A ``)`` that shell COMMAND GRAMMAR makes ordinary must not end a body.

    ``_substitution_bodies`` is the shared answer to "what text does this command
    run as a shell", so a body that stops early is not one pass's problem: every
    consumer inherits the blindness. Quoting was already handled -- the span walk
    reads ``_iter_shell_chars`` -- but two constructs put a literal ``)`` in front
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
