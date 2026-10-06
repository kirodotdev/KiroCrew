"""Tests for the quote-aware argv windows in the self-protection floor.

The self-protection argv windows bound the current command with
``_substitution_depth_delta``, which counts parens on DE-QUOTED tokens, so a
QUOTED ``)`` nested in a command substitution reads as a real closer and the
window ends early at a separator that is still inside the substitution.  For
``_is_credential_mint`` that drops the ``token`` verb from a command bash
actually mints (the correctness defect fixed here).  The sibling operand/kill
windows are pinned too, with the verdict for each.

Program and verb literals are assembled from pieces so the mint shape only
exists at runtime, never as a literal substring of this source or of any
command line.
"""

import pytest

from kiro_crew.security import is_denied
from kiro_crew.security import shell_normalizer as sn

# Assembled from pieces so the credential-mint shape exists only at runtime,
# never as a literal substring of this file or of any command line.
_N = "kiro" + "crew"  # product CLI program name
_MOD = "kiro" + "_crew"  # product import/module name
_T = "to" + "ken"  # the credential-minting verb


class TestRawSimpleCommands:
    """The quote-aware neutralizer collapses each substitution span to one inert
    placeholder word and copies everything else verbatim."""

    def _ph(self):
        return sn._SUBSTITUTION_PLACEHOLDER

    def test_command_substitution_span_collapses_to_placeholder(self):
        # The decoy shape: the quoted ')' and nested ';' live inside the span, so
        # the whole span -- including them -- becomes one inert word.
        assert sn._neutralize_substitution_spans("foo $(true ')' ; true) baz") == (
            f"foo {self._ph()} baz"
        )

    def test_backtick_span_collapses(self):
        assert sn._neutralize_substitution_spans("foo `printf ';'` baz") == (
            f"foo {self._ph()} baz"
        )

    def test_process_substitution_span_collapses(self):
        assert sn._neutralize_substitution_spans("cat <(echo ';') x") == (f"cat {self._ph()} x")

    def test_nested_spans_collapse_to_one_placeholder(self):
        assert sn._neutralize_substitution_spans("foo $(a ; $(b ; c) ; d) baz") == (
            f"foo {self._ph()} baz"
        )

    def test_quoted_dollar_paren_is_literal_not_a_span(self):
        # Inside single quotes ``$(`` is data, so nothing is neutralized.
        src = "echo '$(true)' x"
        assert sn._neutralize_substitution_spans(src) == src

    def test_text_without_substitutions_is_unchanged(self):
        src = 'kirocrew restart; echo "a|b" \\$kept'
        assert sn._neutralize_substitution_spans(src) == src

    def test_surrounding_quotes_and_escapes_preserved_verbatim(self):
        # A STANDALONE span (spaces around it) collapses to the placeholder while
        # the surrounding text is copied byte for byte.
        assert sn._neutralize_substitution_spans("x a $(b) c y") == (f"x a {self._ph()} c y")
        # A span inside a quoted word is part of that word, so it collapses to
        # NOTHING (its empty-output case fuses the word), surroundings verbatim.
        assert sn._neutralize_substitution_spans('x "a$(b)c" y') == 'x "ac" y'


class TestCredentialMintQuoteBlindWindow:
    """Site 1 (the correctness defect this PR fixes).

    The de-quoted mint-verb window closes at a separator NESTED in a command
    substitution once a QUOTED ``)`` has driven its depth counter non-positive,
    so the verb that follows the substitution is never seen -- while bash
    expands the substitution to nothing and runs the mint.
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            # The core exploit: $(true ')' ; true) expands to NOTHING (true
            # ignores its ')' arg), so bash runs ``<cli> <verb>`` -- a real mint
            # -- while the quoted ')' + nested ';' close the de-quoted window early.
            "{n} $(true ')' ; true) {t}",
            # The backtick spelling of the same decoy.
            "{n} `true ')' ; true` {t}",
            # A redirection interposed between the decoy and the verb is still
            # the same simple command: bash runs ``<cli> <verb>`` with stderr
            # redirected.
            "{n} $(true ')' ; true) 2>/tmp/out {t}",
            # The module form shares the identical window, so it must be caught too.
            "python -m {mod} $(true ')' ; true) {t}",
            # Nested one wrapper deep: the floor descends shell payloads.
            "bash -c \"{n} $(true ')' ; true) {t}\"",
            # A quoted ')' with a nested '|' is the same mechanism (| ends the
            # de-quoted window just as ';' does).
            "{n} $(true ')' | true) {t}",
        ],
    )
    def test_decoyed_mint_is_denied(self, cmd):
        assert is_denied(cmd.format(n=_N, mod=_MOD, t=_T))

    @pytest.mark.parametrize(
        "cmd",
        [
            # The undecorated mint the floor has always caught.
            "{n} {t}",
            # Flags interposed, no decoy -- the plain window already catches it.
            "{n} -v {t}",
        ],
    )
    def test_control_mints_remain_denied(self, cmd):
        assert is_denied(cmd.format(n=_N, t=_T))

    @pytest.mark.parametrize(
        "cmd",
        [
            # The verb is an ARGUMENT of a data-consumer command, not the CLI's
            # own argv -- bash prints it, nothing mints.  The fix must not widen
            # the window into denying these.
            "echo {n} $(true ')' ; true) {t}",
            # The name is a path and the verb is a different program's argument.
            "cd /workplace/user/{n}-wt && pytest test/test_{t}_auth.py",
            # The verb lands in grep's argv across a pipe, not the CLI's.
            "{n} doctor | grep {t}",
            # A redirection to a FILE named like the verb is not the verb word.
            "{n} >{t}",
            "{n} 2>{t}.log doctor",
            # The decoy belongs to a DIFFERENT, non-self command on the line.
            "{n} doctor; echo $(true ')' ; true) {t}",
        ],
    )
    def test_scoped_non_detections_stay_allowed(self, cmd):
        assert is_denied(cmd.format(n=_N, t=_T)) is None


class TestSelfKillByNameQuoteBlindWindow:
    """Site 3 -- the pkill/killall BY-NAME window, also fixed here.

    The issue judged this low-reachability assuming a decoy that OUTPUTS text
    (a second pattern pkill rejects).  But an EMPTY-output generator makes
    ``pkill -f $(true ')' ; true) <name>`` expand to ``pkill -f <name>`` -- a
    real self-kill -- while the quoted ')' closes the de-quoted window at the
    nested ';' before the name.  So it is a genuine bypass, caught the same way
    as the mint window.
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            "pkill -f $(true ')' ; true) {n}",
            "killall $(true ')' ; true) {n}",
            "pkill -f `true ')' ; true` {n}",
            "bash -c \"pkill -f $(true ')' ; true) {n}\"",
        ],
    )
    def test_decoyed_self_kill_by_name_is_denied(self, cmd):
        assert is_denied(cmd.format(n=_N))

    @pytest.mark.parametrize(
        "cmd",
        [
            # The undecorated control the floor has always caught.
            "pkill -f {n}",
            "pkill -f '[k]irocrew'",
        ],
    )
    def test_control_self_kills_remain_denied(self, cmd):
        assert is_denied(cmd.format(n=_N))

    @pytest.mark.parametrize(
        "cmd",
        [
            # pkill named as a data-consumer's argument prints words; nothing dies.
            "echo pkill $(true ')' ; true) {n}",
            # the pkill and the name are in DIFFERENT commands.
            "pkill other; echo {n}",
        ],
    )
    def test_scoped_non_detections_stay_allowed(self, cmd):
        assert is_denied(cmd.format(n=_N)) is None


class TestSiblingSiteNotTheSameDefect:
    """Site 2 -- the self-SUBCOMMAND operand window -- LOOKS like a copy of the
    quote-blind defect but is not independently fixable by it, so it is PINNED.

    The subcommand floor is PRECISE, not conservative: it fires only when the
    destructive subcommand is the LEADING operand.  When a substitution precedes
    the subcommand, bash's real leading operand is the substitution's OUTPUT,
    which a static scan cannot evaluate -- so the lead is unprovable and the floor
    must allow, exactly as it already allows the plain ``<cli> $(cmd) restart``.
    A quote-aware window yields the SAME allow (operands ``['<span>', 'restart']``,
    lead is the opaque span), so the quoted-paren window closing early flips no
    deny into an allow here.

    KNOWN RESIDUAL (pinned below): an EMPTY-output generator before the
    subcommand (``<cli> $(true ')' ; true) restart`` -> bash ``<cli> restart``)
    is a real self-restart the floor misses -- but it is the pre-existing
    static-analysis limitation shared with every ``<cli> $(cmd) restart``, NOT the
    quote-blind-window defect, and closing it requires making the subcommand floor
    conservative (a policy change tracked as a follow-up), not a quote-aware read.
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            # The decoyed form and the plain-substitution form share one verdict:
            # the lead is an unevaluatable substitution either way.
            "{n} $(true ')' ; true) restart",
            "{n} $(true) restart",
        ],
    )
    def test_subcommand_behind_substitution_is_allowed_either_way(self, cmd):
        assert is_denied(cmd.format(n=_N)) is None

    def test_plain_restart_with_no_hidden_lead_still_denied(self):
        assert is_denied("{n} restart".format(n=_N))


class TestUnionExercisesTheRawPath:
    """Prove the decoyed denials come from the NEUTRALIZED pass, not the plain
    de-quoted window: with neutralization disabled (``_allow_neutralize=False``)
    the window truncates on the decoy and misses it, while the full call catches
    it.  This is the mutation check -- disabling the pass is exactly what the fix
    would revert to."""

    def test_disabling_neutralize_misses_the_decoyed_mint(self):
        from kiro_crew.security import argv_floor as af

        cmd = "{n} $(true ')' ; true) {t}".format(n=_N, t=_T).lower()
        # The quote-blind window truncates at the nested ';', so disabling the
        # neutralized pass misses it.
        assert af._is_credential_mint(cmd, _allow_neutralize=False) is False
        # The full call's neutralized pass restores the detection.
        assert af._is_credential_mint(cmd) is True
        # The undecoyed control is caught by the window directly.
        assert af._is_credential_mint("{n} {t}".format(n=_N, t=_T)) is True

    def test_disabling_neutralize_misses_the_decoyed_self_kill(self):
        from kiro_crew.security import argv_floor as af

        cmd = "pkill -f $(true ')' ; true) {n}".format(n=_N).lower()
        assert af._is_self_kill(cmd, _allow_neutralize=False) is False
        assert af._is_self_kill(cmd) is True
        assert af._is_self_kill("pkill -f {n}".format(n=_N)) is True


class TestRawWindowResolvesShellWordsNotRawBytes:
    """The raw window must match what bash PASSES, not the raw spelling: a
    backslash escape is decoded, a redirect target is skipped, and a
    substitution span's interior is opaque.  Each case here is one bash runs the
    way the assertion says, confirmed on this checkout.
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            # A backslash before an ordinary letter is removed by bash, so
            # ``kiro\\crew`` is the program/pattern ``kirocrew``.  The de-quoted
            # window truncates on the decoy, so only the raw window sees these --
            # it must decode the backslash before matching the name/verb.
            "pkill -f $(true ')' ; true) kiro\\crew",
            "{n} $(true ')' ; true) to\\ken",
            "{n} $(true ')' ; true) kiro\\crew to\\ken",
        ],
    )
    def test_backslash_decoded_decoy_is_denied(self, cmd):
        assert is_denied(cmd.format(n=_N))

    @pytest.mark.parametrize(
        "benign",
        [
            # A decoy before a BENIGN subcommand stays allowed: bash runs
            # ``<cli> doctor``, no mint verb, no destructive subcommand.
            "{n} $(true ')' ; true) doctor",
            "{n} $(true ')' ; true) --help",
        ],
    )
    def test_decoy_before_benign_subcommand_stays_allowed(self, benign):
        assert is_denied(benign.format(n=_N)) is None

    @pytest.mark.parametrize(
        "tail",
        [
            "doctor > token",
            "doctor 2> token",
        ],
    )
    def test_redirect_decoy_matches_the_decoy_free_verdict(self, tail):
        # ``> token`` as a SEPARATE word is a redirect to a file named ``token``.
        # Whatever the core decides for the decoy-FREE command, the neutralized
        # decoy form must decide identically -- the decoy expands to nothing, so
        # it cannot change the verdict.  (The core's own treatment of a file
        # literally named ``token`` is pre-existing and not this PR's to change;
        # this pins that the fix introduces no NEW divergence.)
        decoyed = "{n} $(true ')' ; true) {tail}".format(n=_N, tail=tail)
        plain = "{n} {tail}".format(n=_N, tail=tail)
        assert (is_denied(decoyed) is None) == (is_denied(plain) is None)

    @pytest.mark.parametrize(
        "cmd",
        [
            # The name appears only INSIDE a substitution whose OUTPUT is an
            # ordinary word, so bash passes ``pkill -f ordinary`` -- not a
            # self-kill.  The neutralized pass must treat the span as opaque, not
            # literal-match its interior source.  (A ``;`` before the name keeps
            # the public regex tier -- whose ``[^;&#>]*`` class stops at it --
            # out of the verdict, so this exercises the structural floor alone.)
            "pkill -f $(true ')' ; true {n}; printf ordinary)",
        ],
    )
    def test_opaque_substitution_span_interior_is_not_the_target(self, cmd):
        assert is_denied(cmd.format(n=_N)) is None

    def test_empty_substitution_glue_still_resolves_the_name(self):
        # An empty substitution vanishes, so ``kiro$()crew`` IS the name
        # ``kirocrew`` -- the core's own empty-substitution resolution catches it
        # (the neutralized pass need not, and must not swallow it).
        assert is_denied("pkill -f kiro$()crew")


class TestNeutralizeHandlesNestingEmptyAndComments:
    """The neutralized pass must reach nested payloads, collapse an EMPTY
    substitution to nothing, and skip a commented opener -- the three ways the
    first neutralize cut missed a command bash still runs."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # F1 -- the decoy is in a NESTED bash -c payload (quote-protected at
            # the outer level), so the outer-only neutralize missed it; each
            # payload source must be neutralized.
            "bash -c 'pkill -f $(true \")\" ; true) {n}'",
            "bash -c \"{n} $(true ')' ; true) {t}\"",
            "sh -c 'pkill -f $(true \")\" ; true) {n}'",
        ],
    )
    def test_nested_payload_decoy_is_denied(self, cmd):
        assert is_denied(cmd.format(n=_N, t=_T))

    @pytest.mark.parametrize(
        "cmd",
        [
            # F2 -- the name is AFTER the decoy (so the de-quoted window truncates)
            # AND glued to an empty ``$()`` (so a placeholder would hide it); the
            # empty substitution must collapse to nothing: ``kiro$()crew`` ->
            # ``kirocrew``.
            "pkill -f $(true ')' ; true) kiro$()crew",
            "{n} $(true ')' ; true) to$()ken",
        ],
    )
    def test_empty_substitution_after_decoy_is_denied(self, cmd):
        assert is_denied(cmd.format(n=_N))

    def test_empty_substitution_collapses_to_nothing(self):
        # Direct check on the neutralizer: an empty span rejoins its neighbours.
        assert sn._neutralize_substitution_spans("kiro$()crew") == "kirocrew"
        assert sn._neutralize_substitution_spans("to`  `ken") == "token"
        # A NON-empty span GLUED to a word also collapses to nothing (its
        # empty-output case fuses the word), while a STANDALONE one is the
        # inert placeholder.
        assert sn._neutralize_substitution_spans("kiro$(x)crew") == "kirocrew"
        assert sn._neutralize_substitution_spans("a $(x) b") == (
            f"a {sn._SUBSTITUTION_PLACEHOLDER} b"
        )

    @pytest.mark.parametrize(
        "cmd",
        [
            # F3 -- a commented ``$(`` with no close must not be read as an
            # unterminated span that swallows the real command after the newline.
            "# $(\npkill -f $(true ')' ; true) {n}",
            "# $(\n{n} $(true ')' ; true) {t}",
        ],
    )
    def test_commented_opener_does_not_swallow_the_next_line(self, cmd):
        assert is_denied(cmd.format(n=_N, t=_T))

    def test_commented_opener_is_skipped_by_the_neutralizer(self):
        # The comment is copied verbatim and the real command on the next line
        # survives (its span neutralized), rather than the whole tail vanishing.
        out = sn._neutralize_substitution_spans("# $(\npkill -f $(x) foo")
        assert out.splitlines()[0] == "# $("
        assert "pkill -f" in out and sn._SUBSTITUTION_PLACEHOLDER in out


class TestNeutralizeFoldsContinuationsAndTracksWordBoundaries:
    """The neutralizer must fold a line-continuation before scanning openers,
    and treat ``#`` as a comment only at a genuine word boundary -- the two ways
    a decoy slipped the first comment/span handling."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # F1 -- ``$`` + backslash-newline + ``(`` folds to ``$(`` in bash, so
            # the opener must be seen after folding.
            "pkill -f $\\\n(true ')' ; true) {n}",
            "{n} $\\\n(true ')' ; true) {t}",
        ],
    )
    def test_line_continuation_opener_decoy_is_denied(self, cmd):
        assert is_denied(cmd.format(n=_N, t=_T))

    def test_neutralizer_folds_continuation_before_scanning(self):
        # The folded span collapses; without folding the ``$\<LF>(`` is invisible.
        assert sn._neutralize_substitution_spans("a $\\\n(x) b") == (
            f"a {sn._SUBSTITUTION_PLACEHOLDER} b"
        )

    @pytest.mark.parametrize(
        "cmd",
        [
            # F2 -- ``$(true)#x`` is ONE word to bash (the ``#`` is glued to the
            # substitution's close), so it is NOT a comment, and the decoyed
            # self-kill after the ``;`` must still be seen.
            "echo $(true)#x; pkill -f $(true ')' ; true) {n}",
            "echo $(true)#x; {n} $(true ')' ; true) {t}",
        ],
    )
    def test_glued_hash_after_substitution_is_not_a_comment(self, cmd):
        assert is_denied(cmd.format(n=_N, t=_T))

    def test_hash_glued_to_a_span_stays_in_the_word(self):
        # The ``#x`` is word data after the span, not a comment, so the ``;`` and
        # the command after it survive neutralization.
        out = sn._neutralize_substitution_spans("echo $(true)#x; pkill -f $(y) foo")
        assert "#x" in out
        assert "pkill -f" in out

    def test_hash_at_a_real_word_boundary_is_still_a_comment(self):
        # A ``#`` after whitespace IS a comment: the span after it is dropped.
        out = sn._neutralize_substitution_spans("echo a # $(x) rest")
        assert out == "echo a # $(x) rest" or out.startswith("echo a # ")


class TestGluedSpanCollapsesToNothing:
    """A substitution span GLUED to the verb/name (no separating space) must
    collapse to nothing, because an empty-output generator fuses the neighbours
    in bash -- ``$(true)token`` -> ``token``.  A STANDALONE span stays a
    placeholder so the subcommand hidden-lead allowance is preserved."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # the verb glued to the RIGHT of the decoy span
            "{n} $(true ')' ; true)token",
            # backtick spelling, glued
            "{n} `true ')' ; true`token",
            # double-quoted span glued to the verb
            "{n} \"$(true ')' ; true)\"token",
            # module form, span glued to the verb
            "python -m {mod} $(true ')' ; true)token",
            # kill: span glued to the LEFT of the name (also exercises decode)
            "pkill -f $(true ')' ; true)kiro{bs}crew",
            "pkill -f $(true ')' ; true)kirocrew",
        ],
    )
    def test_glued_decoy_is_denied(self, cmd):
        assert is_denied(cmd.format(n=_N, mod=_MOD, bs=chr(92)))

    def test_neutralizer_collapses_glued_span_to_nothing(self):
        # right-glued
        assert sn._neutralize_substitution_spans("x $(y)token") == "x token"
        # left-glued
        assert sn._neutralize_substitution_spans("kiro$(y)crew z") == "kirocrew z"
        # standalone stays a placeholder (keeps the operand slot)
        assert sn._neutralize_substitution_spans("x $(y) z") == (
            f"x {sn._SUBSTITUTION_PLACEHOLDER} z"
        )

    def test_standalone_decoy_before_subcommand_stays_allowed(self):
        # The hidden-lead pin: a STANDALONE span before ``restart`` is the lead
        # operand (not ``restart``), so this stays allowed -- the glued-span fix
        # must not collapse a standalone span and flip this to a deny.
        assert is_denied("{n} $(true ')' ; true) restart".format(n=_N)) is None
        assert is_denied("{n} restart".format(n=_N))


class TestNestedAndQuotedSpanBypasses:
    """Round-5 findings: a double-quoted STANDALONE span must stay a placeholder
    (not collapse to nothing and stop the scan), and a nested substitution inside
    double quotes must not close the outer span early."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # Opus F2 -- the span is the whole content of a double-quoted word.
            # It must become a placeholder INSIDE the quotes, so the verb/name
            # after it is still scanned.  (echo -v / echo -f are flags bash passes
            # through, so bash runs ``kirocrew -v token`` / ``pkill -f kirocrew``.)
            "{n} \"$(true ')' ; echo -v)\" token",
            "pkill \"$(true ')' ; echo -f)\" {n}",
            "{n} \"$(true ')' ; true)\" token",
        ],
    )
    def test_double_quoted_standalone_span_is_denied(self, cmd):
        assert is_denied(cmd.format(n=_N))

    @pytest.mark.parametrize(
        "cmd",
        [
            # GPT F1 -- a nested ``$(`` inside double quotes within the outer
            # ``$(...)`` must not close the outer span at the inner quoted ``)``.
            'pkill -f $(true "$(true ")" ; true)" ; true) {n}',
            '{n} $(true "$(true ")" ; true)" ; true) token',
        ],
    )
    def test_nested_double_quoted_substitution_is_denied(self, cmd):
        assert is_denied(cmd.format(n=_N))

    def test_matching_close_paren_consumes_nested_quoted_substitution(self):
        # The shared scanner now spans the WHOLE outer substitution, not stopping
        # at the inner quoted ``)``.
        body = 'true "$(true ")" ; true)" ; true) rest'
        rel, proven = sn._matching_close_paren(body, 0)
        assert proven
        # everything up to and including the outer ``)`` is the span; ``rest`` is after
        assert body[rel:].strip() == "rest"
