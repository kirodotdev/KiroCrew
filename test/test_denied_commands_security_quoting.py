"""Tests that a re-spelt command still meets the deny catalog.

A rule authored as a command shape keeps matching when a token is re-spelt through
quoting, ANSI-C quoting or a nested shell payload, and an empty quoted word, which the shell
hands the program as a real argv element, does not walk a command past the catalog.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401

from kiro_crew import security
from kiro_crew.security import is_denied

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestDenyMatchingIsQuoteNormalized:
    """A rule authored as a command SHAPE must survive re-spelling of a token.

    Both deny tiers match TEXT, and a shell strips quoting, de-escapes, collapses
    empty-string splices and collapses whitespace runs before the program sees
    its argv -- so ``rm -rf "/"`` runs exactly what ``rm -rf /`` runs while
    containing none of that rule's own text.  Of the ~140 built-in rules only the
    six self-protection rules and git-publish had an argv-structural floor
    closing this; every other rule was spelling-dependent.
    ``_deny_segment_views`` adds a quote/escape-normalized re-join of each
    segment as a SECOND view, additively.
    """

    # One rule (``rm -rf /.*``), every spelling a shell reduces to ``rm -rf /``.
    # Deliberately no ``$HOME`` / ``~`` spelling here: the view does not expand,
    # so those belong to the path-identity layer, not to this one.
    RESPELLINGS = (
        'rm -rf "/"',
        "rm -rf '/'",
        '"rm" -rf /',
        "'rm' -rf /",
        'rm "-rf" /',
        "rm '-rf' /",
        "r''m -rf /",
        'r""m -rf /',
        "rm -r''f /",
        "rm  -rf  /",  # whitespace run, not quoting
        "rm\t-rf /",  # tab
        "rm -rf \\/",  # backslash escape
    )

    def test_every_respelling_of_one_rule_is_denied(self):
        for cmd in self.RESPELLINGS:
            reason = is_denied(cmd)
            assert reason is not None, f"quoted respelling escaped the rule: {cmd!r}"

    def test_the_respellings_are_a_real_bypass_without_the_normalized_view(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The additive-proof twin: with the extra view removed, every cell above
        is ALLOWED.

        This is what makes the assertion above a security property rather than an
        incidental match -- if a later change makes the raw text match these on
        its own, this test fails loudly and the cross above stops proving
        anything.
        """
        from kiro_crew import security
        from kiro_crew.security import rm_floor

        monkeypatch.setattr(
            security, "_deny_segment_views", lambda segment, emit_self=True: (segment.lower(),)
        )
        # The argv floor reads quote-removed argv on its own, so it is switched off
        # here to keep this cross about the normalized VIEW alone.
        monkeypatch.setattr(rm_floor, "_RM_FLOOR_BY_KIND", {})
        for cmd in self.RESPELLINGS:
            assert security.is_denied(cmd) is None, (
                f"raw text now matches {cmd!r} on its own -- the cross above no longer "
                "isolates the normalized view"
            )
        # ...while the canonical spelling never needed the view.
        assert security.is_denied("rm -rf /") is not None

    def test_other_rule_families_are_covered_too(self):
        """Not an ``rm``-specific patch: any command-shape rule gains the view."""
        for cmd in (
            'dd "if=/dev/zero" of=/dev/sda',
            "dd if''=/dev/zero of=/dev/sda",
            'chmod "777" /tmp/x',
        ):
            assert is_denied(cmd) is not None, cmd

    def test_respelling_inside_a_compound_command_is_denied(self):
        """The evasion in its own segment after a separator still lands."""
        for cmd in (
            'ls -la && "rm" -rf /',
            'echo start; rm -rf "/"',
            'true | r""m -rf /',
        ):
            assert is_denied(cmd) is not None, cmd

    def test_nested_shell_payloads_are_viewed_in_their_own_right(self):
        """A shell's ``-c`` argument is a COMMAND, and ``shlex`` strips only the
        OUTER quoting level -- so the payload's own inner quoting survives the
        parent's re-join and the rule still misses it.  Every literal payload spelling
        ``_nested_shell_payloads`` recognises must therefore be viewed too.
        """
        for cmd in (
            "bash -c 'dd \"if=/dev/zero\" of=/dev/sda'",
            "sh -c 'rm -rf \"/\"'",
            "sh -c \"rm -rf '/'\"",
            "bash -c -- 'rm -rf \"/\"'",  # ``--`` ends option parsing
            "eval 'rm -rf \"/\"'",
            "bash <<< 'rm -rf \"/\"'",  # herestring feeds the script on stdin
            "env -S 'rm -rf \"/\"'",
            'bash -c \'sh -c "rm -rf \\"/\\""\'',  # two levels of nesting
        ):
            assert is_denied(cmd) is not None, f"nested payload escaped the rule: {cmd!r}"

    def test_a_nested_payload_is_split_before_it_is_viewed(self):
        """The payload is a command LINE, so the separator rule applies inside it
        too -- otherwise the walk fabricates a command one level down."""
        for cmd in (
            "bash -c 'echo rm; -rf /'",
            "bash -c 'echo hello'",
            "sh -c 'ls -la'",
        ):
            assert is_denied(cmd) is None, f"fabricated a command inside a payload: {cmd!r}"

    def test_a_data_consumer_mention_is_not_walked(self):
        """``echo bash -c '<script>'`` PRINTS the script, so descending into it
        would refuse a command that runs nothing (advisory from the GPT 5.6
        lane).  The repo's own ``_data_consumer_exempt`` decides this."""
        for cmd in (
            "echo bash -c 'rm -rf \"/\"'",
            "cat bash -c 'rm -rf \"/\"'",
        ):
            assert is_denied(cmd) is None, f"mention over-blocked: {cmd!r}"

    def test_the_exemption_does_not_weaken_the_raw_tier(self):
        """The unquoted mention was already refused BEFORE this change, because
        the raw text contains the rule's own text.  The exemption must not walk
        that back -- it only decides whether to DESCEND into a payload."""
        for cmd in (
            "echo rm -rf /",
            "echo bash -c 'rm -rf /'",
        ):
            assert is_denied(cmd) is not None, cmd

    def test_executor_wrappers_are_still_walked(self):
        """Guard against narrowing the descent to "launcher in command position",
        the remedy suggested alongside the advisory: in every command below the
        launcher is NOT in command position, and every one of them really
        executes the payload.  A position rule would trade one false positive for
        six bypasses."""
        for cmd in (
            "sudo bash -c 'rm -rf \"/\"'",
            "timeout 5 bash -c 'rm -rf \"/\"'",
            "nohup bash -c 'rm -rf \"/\"'",
            "ssh host bash -c 'rm -rf \"/\"'",
            "xargs bash -c 'rm -rf \"/\"'",
            "env FOO=1 bash -c 'rm -rf \"/\"'",
        ):
            assert is_denied(cmd) is not None, f"executor wrapper escaped the rule: {cmd!r}"

    def test_a_normalized_match_records_the_raw_spelling_too(self, monkeypatch: pytest.MonkeyPatch):
        """Forensics needs both halves.  The view names the command that WOULD
        have run; only the raw spelling shows the evasion.  The full input is
        already in ``operation``, so what the extra field adds is WHICH segment
        normalized into the match.
        """
        from kiro_crew import security

        events: list[dict] = []
        monkeypatch.setattr(
            security,
            "_emit_deny_event",
            lambda tool, pattern, segment, raw_segment="": events.append(
                {"pattern": pattern, "segment": segment, "raw_segment": raw_segment}
            ),
        )
        assert security.is_denied("ls -la && r''m -rf /") is not None
        assert events, "no deny event was emitted"
        last = events[-1]
        assert last["segment"] == "rm -rf /", last
        assert last["raw_segment"] == "r''m -rf /", last

        # A raw match is caught by the WHOLE-STRING pass 1, which has no segment
        # to normalize, so the extra field stays absent and an ordinary denial's
        # event does not grow.
        events.clear()
        assert security.is_denied("ls -la && rm -rf /") is not None
        assert events[-1]["segment"] == "ls -la && rm -rf /"
        assert events[-1]["raw_segment"] == ""

    def test_the_synthesized_target_keeps_model_authored_quoting(self):
        """DOCUMENTED GAP, pinned rather than claimed.

        The PR that added this view asserted that
        ``is_denied_synthesized_target`` needs no normalized view because its
        input is gate-constructed rather than shell text.  That assumption does
        not hold: the ``path`` VALUE is
        model-authored, and ``_normalize_search_path`` resolves home variables and
        dot segments but not quoting, so a quote character survives into the
        synthesized target and a path-keyed operator rule can miss it the same way
        the shell tiers can.

        That is a second surface with its own semantics (a synthesized grammar,
        not a command line) and its own review surface, so it is NOT fixed here --
        it is recorded as a residual in ``docs/system-specs/modules/security.md``
        and pinned here so the gap is findable instead of implied.  When it is
        closed, this test is the one that must flip.
        """
        from kiro_crew import hooks

        target = hooks._search_deny_target(
            {
                "operation": "search_codebase_map",
                "path": '"$HOME"/notes',
                "max_depth": 3,
            }
        )
        assert target, "the synthesizer produced no target for a recursive operation"
        assert '"' in target, (
            "quoting no longer survives into the synthesized target -- the gap this "
            "pins is closed, so update the residual in the security spec and flip "
            "this assertion"
        )

    def test_a_payload_glued_inside_one_token_is_handled(self):
        """A payload is not always a TOKEN.  ``_nested_shell_payloads`` also returns
        SYNTHESIZED text -- a ``sed`` ``e``-flag replacement, a glued herestring
        tail, a glued ``env -S`` argument, an ``alias`` assignment -- so recovering
        a position with ``list.index`` raised ``ValueError`` straight out of the
        permission gate.  Found independently as BLOCKING by the GPT 5.6 and Opus
        4.8 lanes; ``sed 's/x/y/e' notes.txt`` is Opus's reproducer and is
        legitimate input.
        """
        for cmd in (
            "bash<<<'rm -rf \"/\"'",  # glued herestring
            "alias x='rm -rf \"/\"'",  # alias assignment
            "sed 's#x#rm -rf \"/\"#e' file",  # sed e-flag script executes
        ):
            assert is_denied(cmd) is not None, f"glued payload escaped the rule: {cmd!r}"
        for cmd in (
            "sed 's/x/y/e' notes.txt",  # Opus's reproducer -- must not crash, must allow
            "bash<<<'echo hello'",
            "env -S'echo hello'",
        ):
            assert is_denied(cmd) is None, f"benign glued payload over-blocked: {cmd!r}"

    def test_the_exemption_is_decided_per_occurrence_and_fails_closed(self):
        """A payload with no token position cannot be proven inert, so it is
        descended into rather than skipped -- deciding from one recovered index
        would not be sound, because a short synthesized payload can also be a
        coincidental substring of an unrelated token."""
        from kiro_crew import security

        # Synthesized payload, no token position: the destructive one is still
        # denied rather than exempted away.
        assert "rm -rf /" in security._deny_segment_views("bash<<<'rm -rf \"/\"'")
        # Exact-token payload under a data consumer: exempt, so no payload view.
        views = security._deny_segment_views("echo bash -c 'rm -rf \"/\"'")
        assert "rm -rf /" not in views, views

    def test_view_construction_never_raises(self, monkeypatch: pytest.MonkeyPatch):
        """The gate must get a security DECISION, never an exception.

        ``_deny_segment_views`` runs inside the PreToolUse gate, so a raising
        helper would be a crash rather than a deny.  Every window is built inside a
        guard, and the raw view is already present before any of them runs -- so a
        failure costs the extra match and nothing else.
        """
        from kiro_crew import security

        def _boom(*_a: object, **_k: object) -> object:
            raise RuntimeError("payload walk exploded")

        for target in (
            "_nested_shell_payloads",
            "_argv_programs",
            "_shell_tokens",
            "_decode_shell_quoted_literals",
        ):
            monkeypatch.setattr(security, target, _boom)
            assert security._deny_segment_views("rm -rf /") == ("rm -rf /",)
            # ...and the raw tier still decides, so the deny stands.
            assert security.is_denied("rm -rf /") is not None
            assert security.is_denied("ls -la && rm -rf /") is not None
            # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing per-target undo inside the loop, moved verbatim; tracked for a de-flake pass
            monkeypatch.undo()

    def test_ansi_c_and_locale_quoting_are_resolved(self):
        """``$'…'`` and ``$"…"`` are QUOTING forms: bash computes the value before
        the program sees it, so ``rm -rf $'/'`` runs exactly what ``rm -rf /`` runs.
        A matcher that has not resolved them is reading a spelling the shell never
        hands over.  BLOCKING from the GPT 5.6 lane; the escape form matters too,
        since ``$'\\x2d\\x72\\x66'`` is ``-rf``.
        """
        for cmd in (
            "rm -rf $'/'",
            "dd $'if=/dev/zero' of=/dev/sda",
            "$'rm' -rf /",
            "rm $'\\x2d\\x72\\x66' /",  # the flag spelled in hex
            "bash -c $'rm -rf \"/\"'",  # ANSI-C wrapping a nested payload
            'rm -rf $"/"',  # locale quoting
        ):
            assert is_denied(cmd) is not None, f"dollar-quoted spelling escaped: {cmd!r}"
        for cmd in (
            "echo $'hello world'",
            "grep $'needle' src/",
        ):
            assert is_denied(cmd) is None, f"benign dollar-quoted command over-blocked: {cmd!r}"

    def test_decoding_dollar_quotes_does_not_eat_variable_references(self):
        """The decode runs on the RAW text and REQUIRES the quote character, which
        is what makes it safe.  After ``shlex`` the quotes are gone and ``$'/'``
        reads as ``$/`` -- indistinguishable from ``$HOME`` -- so a post-shlex
        ``$``-strip would eat real variables and break the path normalizer's own
        ``$HOME`` expansion.
        """
        import os

        from kiro_crew import security

        home = os.path.expanduser("~")
        assert security.normalize_shell_command("cat $HOME/x") == ["cat", f"{home}/x"]
        assert security.normalize_shell_command("cat ${HOME}/x") == ["cat", f"{home}/x"]
        assert security._shell_tokens("echo $FOO") == ["echo", "$FOO"]
        assert security._shell_tokens("echo $(date)") == ["echo", "$(date)"]
        # A decoded value containing whitespace stays ONE token.
        assert security._shell_tokens("echo $'a b'") == ["echo", "a b"]

    def test_unicode_escapes_in_dollar_quotes_are_decoded(self):
        """``$'\\u002d\\u0072\\u0066'`` is ``-rf``, the same word the ``\\x``-spelled
        form already decoded to.  BLOCKING from the GPT 5.6 lane.  The gap was in
        the SHARED escape decoder rather than in this view, so it also closes a
        live bypass of the credential-mint floor -- see the sibling test below.
        """
        from kiro_crew import security

        assert security._decode_printf_escapes(r"\u002d\u0072\u0066") == "-rf"
        assert security._decode_printf_escapes(r"\U0000002d\U00000072") == "-r"
        for cmd in (
            r"rm $'\u002d\u0072\u0066' /",
            r"rm -rf $'\u002f'",
            r"rm $'\U0000002d\U00000072\U00000066' /",
        ):
            assert is_denied(cmd) is not None, f"unicode-escaped spelling escaped: {cmd!r}"

    def test_the_unicode_widths_are_exact_and_case_sensitive(self):
        """Bash consumes AT MOST 4 hex digits after ``\\u`` and 8 after ``\\U``, so
        ``$'\\u0072f'`` is ``r`` followed by a literal ``f`` -- not a 5-digit code
        point.  Reading more digits than the spelling allows is a bypass: the wrong
        character replaces the two the shell passes, and ``rm -$'\\u0072f' /``
        escaped the rule that way (BLOCKING from the GPT 5.6 lane).
        """
        from kiro_crew import security

        assert security._decode_printf_escapes(r"\u0072f") == "rf"
        assert security._decode_printf_escapes(r"\u002d1234") == "-1234"
        assert security._decode_printf_escapes(r"\U0000002d") == "-"
        assert security._decode_printf_escapes(r"\u2d") == "-"
        assert is_denied(r"rm -$'\u0072f' /") is not None

    def test_case_is_preserved_until_after_the_escapes_are_decoded(self):
        """The widths above are case-sensitive, and ``is_denied`` lowercases its
        input -- so the decode has to happen BEFORE that fold.  Segments are
        therefore split from the original-case text, which is safe because no case
        mapping produces a separator, and the ordinary case-insensitive matching
        must still work.
        """
        from kiro_crew import security

        # Splitting commutes with lowercasing.
        mixed = "LS -la && RM -RF / ; Echo Done"
        assert [s.strip().lower() for s in security._split_segments(mixed)] == [
            s.strip() for s in security._split_segments(mixed.lower())
        ]
        # Every view is lowercased regardless of the input's case...
        views = security._deny_segment_views("RM -RF $'/'")
        assert all(v == v.lower() for v in views), views
        assert views[0] == "rm -rf $'/'"
        # ...and an uppercase destructive command is still denied.
        assert is_denied("RM -RF /") is not None
        assert is_denied(r"RM -RF $'\u002F'") is not None

    def test_the_decoder_gap_also_bypassed_the_credential_mint_floor(self):
        """Scope note, pinned: the missing ``\\u`` decoding was NOT introduced by the
        normalized view -- it sat in ``_decode_printf_escapes``, which the
        argv-structural self-protection floors already used.  So the same spelling
        walked past an un-disableable rule while its ``\\x`` twin was refused.  Both
        spellings must read as the same word.
        """
        from kiro_crew import security

        for spelling in (
            "kirocrew $'\\u0074\\u006f\\u006b\\u0065\\u006e'",
            "kirocrew $'\\x74\\x6f\\x6b\\x65\\x6e'",
            "kirocrew token",
        ):
            assert security._is_credential_mint(spelling.lower()), spelling
            assert security.is_denied(spelling) is not None, spelling

    def test_a_lone_surrogate_escape_stays_inert(self):
        """A decoded lone surrogate is not a character bash can pass either, and it
        would travel into the SEL audit record whose JSON encoder raises on it --
        turning a denial into a crash.  Left encoded, like NUL."""
        from kiro_crew import security

        assert security._decode_printf_escapes(r"\ud800") == r"\ud800"
        assert security._decode_printf_escapes(r"\u0000") == r"\u0000"
        # ...and a command carrying one still returns a decision rather than raising.
        assert is_denied(r"echo $'\ud800'") is None

    def test_line_continuations_fold_exactly_where_bash_folds_them(self):
        """The rule is MEASURED, not assumed.  ``printf %q`` on the resulting argv
        gives, for ``<spelling> BB``:

            A\\<nl>A BB      -> <AA><BB>            folded
            "A\\<nl>A" BB    -> <AA><BB>            folded
            'A\\<nl>A' BB    -> <A\\<nl>A><BB>       NOT folded
            $'A\\<nl>A' BB   -> <A\\<nl>A><BB>       NOT folded

        So the fold applies unquoted and inside double quotes, and preserves
        single-quoted and ANSI-C spans.  ``$"..."`` follows the double-quote rule.
        """
        from kiro_crew import security

        fold = security._fold_line_continuations
        assert fold("A\\\nA BB") == "AA BB"
        assert fold('"A\\\nA" BB') == '"AA" BB'
        assert fold("'A\\\nA' BB") == "'A\\\nA' BB"
        assert fold("$'A\\\nA' BB") == "$'A\\\nA' BB"
        assert fold('$"A\\\nA" BB') == '$"AA" BB'
        # ``\<CR><LF>`` is NOT a continuation: the backslash escapes the CR into a
        # literal carriage return and the LF then ENDS the command. Measured --
        # ``printf "%q " A\<CR><LF>A BB`` prints ``$'A\r'`` and then runs ``A`` as a
        # separate command, so the two lines must NOT be joined here.
        assert fold("A\\\r\nA BB") == "A\\\r\nA BB"
        # A backslash escaping something else is untouched, and cannot open a quote.
        assert fold("a\\'b\\\nc") == "a\\'bc"

    def test_folded_continuation_spellings_are_denied(self):
        """``_split_segments`` cuts on the newline, so without folding first the
        continuation is severed and neither piece carries the command bash runs.
        BLOCKING from the GPT 5.6 lane; these are the spellings its probe proved
        bash folds.
        """
        for cmd in (
            '"r\\\nm" -rf /',
            'rm "-r\\\nf" /',
            'rm -rf "\\\n/"',
            "r\\\nm -rf /",
            "rm -rf \\\n/",
        ):
            assert is_denied(cmd) is not None, f"continuation spelling escaped: {cmd!r}"

    def test_the_preserving_contexts_are_not_over_blocked(self):
        """In these the continuation is LITERAL, so the command is not the
        destructive one and must not be refused -- which is why the fold had to be
        quote-aware rather than a bare regex."""
        for cmd in (
            "'r\\\nm' -rf /",  # bash argv: <r\<nl>m> -- a different program name
            "echo 'r\\\nm -rf /'",  # printed literally
            "$'r\\\nm' -rf /",
        ):
            assert is_denied(cmd) is None, f"literal continuation over-blocked: {cmd!r}"

    def test_the_blunt_floor_helper_is_why_the_fold_is_quote_aware(self):
        """Kept as the record of a rejected reuse.

        ``_shell_join_continuations`` already existed and looks like the answer, but
        it is a bare regex that folds inside SINGLE quotes too -- which bash does
        not -- and its own comment scopes it deliberately to the self-protection
        floor's tokenizer input, "NOT a catalog-wide rewrite of the matched text".
        These assertions document what reusing it here would have done, so the
        one-line shortcut is not reached for again.
        """
        from kiro_crew import security

        blunt = security._shell_join_continuations
        assert blunt('"r\\\nm" -rf /') == '"rm" -rf /'  # agrees with bash here...
        assert blunt("'r\\\nm' -rf /") == "'rm' -rf /"  # ...but not here
        assert blunt("echo 'r\\\nm -rf /'") == "echo 'rm -rf /'"  # would over-block
        # The quote-aware fold disagrees with it in exactly the preserving cases.
        assert security._fold_line_continuations("'r\\\nm' -rf /") == "'r\\\nm' -rf /"

    def test_ansi_c_escaped_quotes_are_decoded(self):
        """Inside ``$'…'`` bash resolves ``\\"`` and ``\\'`` to the plain quote, so
        ``bash -c $'rm -rf \\"/\\"'`` hands the inner shell the script ``rm -rf "/"``
        and it runs the destructive command.  Leaving the backslashes in meant the
        nested view missed the rule (BLOCKING from the GPT 5.6 lane).
        """
        for cmd in (
            "bash -c $'rm -rf \\\"/\\\"'",
            "bash -c $'rm -rf \\'/\\''",
        ):
            assert is_denied(cmd) is not None, f"escaped-quote payload escaped: {cmd!r}"
        # ...but the same escapes NOT feeding a shell are a literal operand, and
        # must not be over-blocked: bash argv for ``rm -rf $'\\"/\\"'`` is
        # ``<rm><-rf><\\"/\\">`` -- a file named `"/"`, not the root (printf %q).
        assert is_denied("rm -rf $'\\\"/\\\"'") is None

    def test_the_ansi_c_decoder_is_a_single_pass(self):
        """Sequential replaces let one substitution's OUTPUT be re-read as another's
        input.  ``$'\\\\n'`` is an escaped backslash then the letter ``n`` -- two
        characters -- but resolving ``\\\\`` first and then looking for ``\\n``
        collapses it to whitespace and invents a separator bash never passed.  One
        left-to-right pass makes that impossible.
        """
        from kiro_crew import security

        decode = security._decode_ansi_c_body
        assert decode(r"\\n") == "\\n"  # backslash + n, NOT whitespace
        assert decode(r"\"") == '"'
        assert decode(r"\'") == "'"
        assert decode(r"\?") == "?"
        assert decode(r"\x2d") == "-"
        assert decode(r"\u0072f") == "rf"  # exact width, then a literal f
        assert decode(r"\q") == "\\q"  # unrecognised: both characters kept
        assert decode(r"\n") == " "  # this family has always normalized to a space

    def test_a_nested_payloads_own_continuations_are_folded(self):
        """A payload is a command LINE, so the shell that runs it folds ITS
        continuations before lexing.  Two things were needed: fold the payload
        before splitting it, AND walk the WHOLE command for payloads -- because
        ``_split_segments`` is deliberately quote-unaware, so the newline inside the
        quoted payload severs the command before the ``-c`` script can be extracted
        from it.  BLOCKING from the GPT 5.6 lane.
        """
        for cmd in (
            "bash -c 'r\\\nm -rf /'",
            "bash -c 'rm -r\\\nf /'",
            "bash -c 'rm -rf \\\n/'",
            "eval 'r\\\nm -rf /'",
        ):
            assert is_denied(cmd) is not None, f"nested continuation escaped: {cmd!r}"

    def test_the_whole_command_walk_emits_no_view_of_itself(self):
        """``emit_self=False`` is what keeps the whole-command walk from fabricating
        a command across separators -- it contributes payload views only."""
        from kiro_crew import security

        cmd = "echo one\ntrue && bash -c 'rm -rf \"/\"'"
        views = security._deny_segment_views(cmd, False)
        assert all("echo one" not in v for v in views), views
        assert "rm -rf /" in views, views
        # With emit_self on, the source's own re-join IS the first view.
        assert security._deny_segment_views("ls -la")[0] == "ls -la"

    def test_locale_quoting_uses_double_quote_semantics_not_ansi_c(self):
        """``$"…"`` is locale TRANSLATION, not ANSI-C -- measured, because treating
        the two alike was a bypass (BLOCKING from the GPT 5.6 lane).

        bash gives ``$"\\r\\mAA"`` the word ``\\r\\mAA``, byte-identical to plain
        ``"\\r\\mAA"``: inside double quotes a backslash escapes only ``$``, a
        backtick, ``"``, ``\\`` and a newline, so ``\\r`` is a literal backslash-r
        and NOT a carriage return.  Decoding it as ANSI-C turned that ``\\r`` into
        whitespace and the command vanished from the view -- while the inner shell of
        ``bash -c $"\\r\\m -rf /"`` resolves the backslashes in its own lexing pass
        and runs the destructive command (measured: ``bash -c $"\\r\\mAA"`` executes
        ``rmAA``).
        """
        from kiro_crew import security

        decode = security._decode_shell_quoted_literals
        # Locale: the $ goes, the double-quoted text is left for shlex.
        assert decode('$"\\r\\mAA"') == '"\\r\\mAA"'
        # ANSI-C: the body IS decoded, so the two forms are not interchangeable.
        assert decode("$'\\r\\mAA'") == "' \\mAA'"

        assert is_denied('bash -c $"\\r\\m -rf /"') is not None
        assert is_denied('bash -c $"rm -rf /"') is not None
        # ...and the operand form stays denied, because bash's operand there is `/`.
        assert is_denied('rm -rf $"/"') is not None
        # A benign locale-quoted string is untouched.
        assert is_denied('echo $"hello world"') is None

    def test_ansi_c_control_escapes_are_decoded(self):
        """``\\cX`` is a control character and ``\\cI`` is a TAB, so
        ``bash -c $'rm\\cI-rf /'`` hands the inner shell a tab-separated
        ``rm -rf /`` and it runs -- measured, the inner shell does split on it
        (BLOCKING from the GPT 5.6 lane).

        The mapping is MEASURED, not derived: bash gives ``ord(upper(X)) & 0x1F``
        with ``?`` special-cased to 0x7F.  An XOR-0x40 guess gets ``\\c0`` wrong --
        bash yields 0x10, not the letter ``p``.
        """
        from kiro_crew import security

        decode = security._decode_ansi_c_body
        # Every control result takes the same normalization to a space as the named
        # family, which is what puts a token boundary where the shell puts one.
        assert decode(r"a\cIb") == "a b"  # 0x09 tab
        assert decode(r"a\cJb") == "a b"  # 0x0a newline
        assert decode(r"a\cMb") == "a b"  # 0x0d carriage return
        assert decode(r"a\c0b") == "a b"  # 0x10 -- not 'p'
        assert decode(r"a\c?b") == "a b"  # 0x7f
        assert decode(r"a\c[b") == "a b"  # 0x1b escape
        # ...and a NUL TRUNCATES the word, which is what bash does with one --
        # measured: `$'AA\\c@junk'` yields `AA`.
        assert decode(r"a\c@b") == "a"

        for cmd in (
            r"bash -c $'rm\cI-rf /'",
            r"bash -c $'rm\cJ-rf /'",
            r"bash -c $'rm\cM-rf /'",
            r"bash -c $'dd\cIif=/dev/zero of=/dev/sda'",
        ):
            assert is_denied(cmd) is not None, f"control-escape payload escaped: {cmd!r}"

    def test_the_quoting_regex_is_not_redos_prone(self):
        """The negated classes must EXCLUDE the backslash.

        With ``[^']`` a backslash can match either alternative -- ``\\\\.`` (two
        characters) or the class (one) -- the textbook ambiguous quoted-string
        pattern, so an unterminated ``$'`` followed by a run of backslashes forces
        the engine through ~1.618**n tilings.  This regex runs inside the
        PreToolUse gate on the full, uncapped command, so that is a hang rather
        than a slowdown (BLOCKING from the Opus 4.8 lane; measured at 9 ms for 24
        backslashes, growing ~1.6x per character added).

        Asserted two ways: structurally, that neither class admits a backslash, and
        with a budget a Fibonacci-time scan could not possibly meet.
        """
        import time

        from kiro_crew import security

        pattern = security._ANSI_C_QUOTE_RE.pattern
        assert "[^'\\\\]" in pattern and '[^\\"\\\\]' in pattern, pattern

        payload = "AA $'" + ("\\" * 2000)
        start = time.perf_counter()
        security._decode_shell_quoted_literals(payload)
        elapsed = time.perf_counter() - start
        # flake-ok: pre-existing 1 s wall-clock ceiling that only separates a linear decode from the Fibonacci-time one, moved verbatim; tracked for a de-flake pass
        assert elapsed < 1.0, f"decode took {elapsed:.3f}s -- the ambiguity is back"

    def test_the_audit_fields_redact_before_truncating(self):
        """``redact_and_truncate``, never a bare slice.

        A credential straddling the 200-char boundary would be cut in half, and the
        fragment does not match the credential pattern -- so SEL's own write-path
        redaction cannot catch it and the partial secret persists in a
        dashboard-readable log.  BLOCKING from the GPT 5.6 lane on the new
        ``raw_segment`` field; the older ``segment`` field carried the same hazard.
        """
        from kiro_crew import security

        captured: list[object] = []

        class _Recorder:
            def log(self, event: object) -> None:
                captured.append(event)

        original = security.SecurityEventLog
        security.SecurityEventLog = _Recorder  # type: ignore[assignment]
        try:
            secret = "AKIA" + "Q" * 16
            padded = "x" * 190 + secret + " rm -rf /"
            security._emit_deny_event("probe", "rm -rf /.*", padded, raw_segment=padded)
        finally:
            security.SecurityEventLog = original  # type: ignore[assignment]

        assert captured, "no event recorded"
        meta = captured[-1].metadata  # type: ignore[attr-defined]
        for field in ("segment", "raw_segment"):
            value = meta.get(field, "")
            assert secret not in value, f"{field} leaked the credential: {value!r}"
            assert secret[:12] not in value, f"{field} leaked a fragment: {value!r}"

    def test_octal_escapes_are_masked_to_one_byte_like_bash(self):
        """MEASURED: ``$'\\555'`` is ``m`` (0o555 & 0xFF == 0x6D), ``$'\\777'`` is
        0xFF, ``$'\\400'`` is a NUL bash cannot place in an argv, and ``$'r\\555'``
        is ``rm``.  Converting the full octal value instead produced ``ŭ`` where
        bash passes ``m``, so ``$'r\\555' -rf /`` ran while the view matched nothing
        (BLOCKING from the GPT 5.6 lane).
        """
        from kiro_crew import security

        decode = security._decode_ansi_c_body
        assert decode(r"\555") == "m"
        assert decode(r"\155") == "m"
        assert decode(r"\777") == "\xff"
        assert decode(r"\101") == "A"
        # A masked value of zero is a NUL, and bash TRUNCATES the word there --
        # measured: `$'AA\\400junk'` yields `AA`, `$'\\400'` the empty word.
        assert decode(r"\400") == ""
        assert is_denied(r"$'r\555' -rf /") is not None

    def test_ansi_c_octal_consumes_three_digits_total_like_bash(self):
        """MEASURED: in ``$'...'`` a leading zero is one of the (at most) three
        octal digits -- ``$'\\06777'`` is ``\\067`` ('7') then the literal ``77``,
        so bash passes ``777``; ``$'\\0677'`` passes ``77``.  The ``\\0nnn``
        four-digit form belongs to ``echo -e``/``printf %b`` only.  Sharing that
        pattern here consumed a fourth digit, so ``chmod $'\\06777' /tmp/x``
        normalized to a one-byte argument instead of ``chmod 777 /tmp/x`` and a
        rule on the decoded spelling missed (BLOCKING from the GPT 5.6 lane).
        """
        from kiro_crew import security

        decode = security._decode_ansi_c_body
        assert decode(r"\06777") == "777"
        assert decode(r"\0677") == "77"
        assert decode(r"\067") == "7"
        # The printf/echo -e decoder keeps the four-digit form: '\0677' there is
        # ONE escape (0o677 & 0xFF == 0xBF), measured against `echo -e`.
        assert security._decode_printf_escapes(r"\0677") == "\xbf"

    def test_a_non_ascii_control_target_does_not_crash_the_gate(self):
        """``str.upper()`` is not length-preserving outside ASCII -- ``"ß".upper()``
        is ``"SS"`` -- so ``ord`` of it raised ``TypeError`` straight out of the
        permission gate on ``echo $'\\cß'`` (BLOCKING from the GPT 5.6 lane).  A
        non-ASCII target keeps both characters, as bash does for an undefined
        spelling.
        """
        from kiro_crew import security

        assert security._decode_ansi_c_body("\\c\u00df") == "\\c\u00df"
        assert is_denied("echo $'\\c\u00df'") is None
        # ...and a non-ASCII operand still yields a DECISION rather than an
        # exception. This one is denied on purpose: the rule blocks a recursive
        # force-delete rooted at the filesystem root, and `/<non-ascii>` is rooted
        # there -- what matters here is that the gate answers at all.
        assert is_denied("rm -rf $'/\u00df'") is not None
        assert is_denied(r"bash -c $'rm\cI-rf /'") is not None

    def test_a_nul_escape_truncates_the_word_like_bash(self):
        """MEASURED on every spelling that can reach zero: ``$'AA\\0junk'``,
        ``$'AA\\400junk'``, ``$'AA\\x00junk'``, ``$'AA\\u0000j'`` and
        ``$'AA\\c@junk'`` all yield ``AA``, and ``$'\\0AA'`` yields the empty word --
        bash cannot place a NUL in an argv, and what it does instead is STOP there.

        Leaving the escape encoded was a bypass:
        ``$'dd\\0junk' if=/dev/zero of=/dev/sda`` ran while the view held
        ``dd\\0junk if=`` and matched nothing (BLOCKING from the GPT 5.6 lane).
        """
        from kiro_crew import security

        decode = security._decode_ansi_c_body
        for body in (r"AA\0junk", r"AA\400junk", r"AA\x00junk", r"AA\u0000j", r"AA\c@junk"):
            assert decode(body) == "AA", body
        assert decode(r"\0AA") == ""
        # The OTHER inert codes keep the escape rather than truncating, because bash
        # does not produce them at all.
        assert decode(r"AA\ud800junk") == r"AA\ud800junk"

        assert is_denied(r"$'dd\0junk' if=/dev/zero of=/dev/sda") is not None
        assert is_denied(r"$'mkfs\0junk' /dev/sda") is not None

    def test_flag_interposition_is_caught_by_the_argv_floor(self):
        """An interposed flag breaks the contiguous catalog text, not the argv.

        ``$'rm\\0junk' -rf --no-preserve-root /`` normalizes to exactly the command
        bash runs, and the rule ``rm -rf /.*`` requires its text contiguous, so no
        view makes the pattern match. The recursive-force argv floor reads the flags
        wherever they sit, so both spellings are refused.
        """
        from kiro_crew import security

        assert is_denied("rm -rf --no-preserve-root /") is not None
        assert is_denied(r"$'rm\0junk' -rf --no-preserve-root /") is not None
        assert is_denied("rm -rf /") is not None
        views = security._deny_segment_views(r"$'rm\0junk' -rf --no-preserve-root /")
        assert "rm -rf --no-preserve-root /" in views, views

    def test_two_accepted_over_blocks_are_pinned_not_implied(self):
        """ACCEPTED residuals from the GPT 5.6 lane's advisory findings.

        Both are FALSE POSITIVES, not bypasses, and both were measured:

        * ``$'…'`` is INERT inside double quotes -- bash's word for
          ``"$'r\\155 -rf /'"`` is the literal ``$'r\\155 -rf /'`` and ``echo``
          prints it verbatim -- but the decode is applied without tracking the
          outer quote context, so a view can hold the decoded text.
        * ``$'r\\155 -rf /'`` is ONE word (``rm -rf /`` with spaces inside it), and
          running it gives "No such file or directory" because no program has that
          name; re-joining tokens with spaces turns those intra-word spaces into
          argv boundaries.

        Accepted rather than fixed, on the asymmetry ``TestNameAsDataIsNotAnInvocation``
        documents for the data-consumer denylist: a false positive is "annoying, visible, and
        safe", while the inverse is a silent bypass -- and ``is_denied``'s own
        docstring states over-blocking is the safer direction for this pass.  Both
        suggested remedies push toward LESS denial, and the second one would have to
        mask intra-token whitespace, which is the mechanism that makes a re-spelled
        command's argv read as the command in the first place.  Pinned so the
        behaviour is findable and deliberate; if either is closed, its assertion
        flips.
        """
        assert is_denied("echo \"$'r\\155 -rf /'\"") is not None
        assert is_denied("echo $'r\\155 -rf /'") is not None

    def test_a_single_segment_command_is_not_walked_twice(self):
        """The whole-command payload walk exists for the case where the split
        SEVERED a quoted payload.  With one segment the whole command IS that
        segment, so walking it twice doubles the payload scan -- which is quadratic
        in token count inside ``_nested_shell_payloads`` -- for no additional view.
        Raised as a stall risk by the GPT 5.6 lane; measured, skipping the duplicate
        halves the cost on a command padded with interpreter tokens.
        """
        from kiro_crew import security

        calls: list[tuple[str, bool]] = []
        real = security._deny_segment_views

        def _spy(segment: str, emit_self: bool = True) -> tuple[str, ...]:
            calls.append((segment, emit_self))
            return real(segment, emit_self)

        security._deny_segment_views = _spy  # type: ignore[assignment]
        try:
            security.is_denied("ls -la")
            single = list(calls)
            calls.clear()
            security.is_denied("ls -la && echo hi")
            compound = list(calls)
        finally:
            security._deny_segment_views = real  # type: ignore[assignment]

        # One segment: exactly one walk, and it is the emitting one.
        assert single == [("ls -la", True)], single
        # Two segments: the whole-command walk is needed, and emits nothing itself.
        assert compound[0] == ("ls -la && echo hi", False), compound
        assert [c[0] for c in compound[1:]] == ["ls -la", "echo hi"], compound
        # ...and the payload reach it exists for is intact.
        assert is_denied("bash -c 'r\\\nm -rf /'") is not None

    def test_unbalanced_quote_still_normalizes_through_the_fallback(self):
        """An unterminated quote makes ``shlex`` raise; the degraded fallback
        (whitespace split + quote strip) must still produce the view, so a
        hostile unparseable spelling is not a bypass."""
        assert is_denied('rm -rf "/') is not None
        assert is_denied("rm -rf '/") is not None

    def test_normalization_failure_cannot_flip_a_deny_to_an_allow(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Fail-closed: the raw view is matched FIRST and independently, so a
        tokenizer that raises loses only the extra match."""
        from kiro_crew import security

        def _boom(cmd: str) -> list[str]:
            raise RuntimeError("tokenizer exploded")

        monkeypatch.setattr(security, "_shell_tokens", _boom)
        assert security._deny_segment_views("rm -rf /") == ("rm -rf /",)
        assert security.is_denied("rm -rf /") is not None
        assert security.is_denied("ls -la && rm -rf /") is not None

    def test_the_view_never_crosses_a_separator(self):
        """Re-joining tokens erases command boundaries, so the view is built per
        SEGMENT.  A whole-input re-join would fabricate a command that was never
        run -- these two inputs are each two commands, neither of which is
        destructive."""
        for cmd in (
            "echo rm\n-rf /",
            "echo rm; -rf /",
            "echo rm && -rf /",
        ):
            assert is_denied(cmd) is None, f"fabricated a command across a separator: {cmd!r}"

    def test_benign_commands_stay_allowed(self):
        """Including quoted ones -- the view only removes quoting, it does not
        invent tokens."""
        for cmd in (
            "ls -la",
            "echo hello",
            'grep -rn "needle" src/',
            "git commit -m 'fix the thing'",
            'python -c "print(1)"',
            "aws s3 ls",
            "git push origin my-feature",
        ):
            assert is_denied(cmd) is None, cmd

    def test_views_are_deduplicated_when_normalization_changes_nothing(self):
        """A command with no quoting/escaping/padding must not pay a second
        140-rule pass."""
        from kiro_crew import security

        assert security._deny_segment_views("ls -la") == ("ls -la",)
        assert security._deny_segment_views('rm -rf "/"') == ('rm -rf "/"', "rm -rf /")
        # A nested payload adds its own view after the parent's, and the walk
        # takes no numeric depth cap -- it terminates because each payload is
        # strictly shorter than its parent's source text.
        assert security._deny_segment_views("sh -c 'rm -rf \"/\"'") == (
            "sh -c 'rm -rf \"/\"'",
            'sh -c rm -rf "/"',
            "rm -rf /",
        )

    def test_shared_tokenizer_left_the_path_normalizer_expanding(self):
        """``_shell_tokens`` was factored OUT of ``normalize_shell_command``; the
        expansion that makes the latter the PATH normalizer must still run, or
        the sensitive-path normalizer pass silently stops resolving spellings."""
        import os

        from kiro_crew import security

        home = os.path.expanduser("~")
        assert security.normalize_shell_command('cat "$HOME"/.ssh/id_rsa') == [
            "cat",
            f"{home}/.ssh/id_rsa",
        ]
        assert security.normalize_shell_command("cat ~/.ssh/id_rsa") == [
            "cat",
            f"{home}/.ssh/id_rsa",
        ]
        # ...and the tokenizer itself deliberately does NOT expand.
        assert security._shell_tokens('cat "$HOME"/.ssh/id_rsa') == ["cat", "$HOME/.ssh/id_rsa"]
        assert security._shell_tokens("") == []


class TestEmptyArgvElementDoesNotBreakTheDenyView:
    """An empty-quoted word must not walk a command past the deny catalog.

    ``rm -rf "" /home/x`` runs exactly what ``rm -rf /home/x`` runs -- the empty
    operand is a real argv element the shell hands over, and ``rm`` simply
    reports it and deletes the rest.  But the deny VIEW is a single-space join of
    argv, so a zero-width element rendered as a spurious extra separator
    (``rm -rf  /home/x``) and every rule authored as a command SHAPE with single
    separators stopped matching its own target.

    The escape was pattern-DEPENDENT, which is what places the repair in the
    render rather than in individual rules: ``chmod "" 777 /etc/passwd`` stayed
    denied only because the rule that catches it tolerates the extra separator.

    The empty-elided render is ADDED as a third view, never substituted for the
    plain join -- ``test_the_elided_view_is_added_and_never_substituted`` carries
    the measured reason.
    """

    # One rule (``rm -rf /.*``), every spelling of an empty word a shell accepts,
    # at every position where it changes the join.
    EMPTY_WORD_SPELLINGS = (
        'rm -rf "" /home/x',
        "rm -rf '' /home/x",
        "rm -rf $'' /home/x",  # ANSI-C quoting, empty body
        'rm -rf $"" /home/x',  # locale quoting, empty body
        "rm -rf \"\"'' /home/x",  # concatenation of two empty words
        "rm -rf ''\"\" /home/x",
        'rm -rf """" /home/x',
        'rm -rf "" "" /home/x',  # two separate empty operands
        'rm "" -rf /home/x',  # between the program and its flag
    )

    def test_every_empty_word_spelling_is_denied(self):
        for cmd in self.EMPTY_WORD_SPELLINGS:
            assert is_denied(cmd) is not None, f"empty word escaped the rule: {cmd!r}"

    def test_the_empty_word_is_a_real_bypass_without_the_elision(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The additive-proof twin, in the shape this file already uses: with the
        normalized view removed, every cell above is ALLOWED -- so the assertion
        above is a property of the view's render and not an incidental raw match.
        """
        from kiro_crew import security

        monkeypatch.setattr(
            security, "_deny_segment_views", lambda segment, emit_self=True: (segment.lower(),)
        )
        for cmd in self.EMPTY_WORD_SPELLINGS:
            assert security.is_denied(cmd) is None, (
                f"raw text now matches {cmd!r} on its own -- the cross above no "
                "longer isolates the view's render"
            )
        assert security.is_denied("rm -rf /home/x") is not None

    def test_other_rule_families_escaped_the_same_way(self):
        """Not an ``rm``-specific patch: any rule whose shape uses single
        separators was defeated by the same word."""
        for cmd in (
            'dd "" if=/dev/zero of=/dev/sda',
            "dd '' if=/dev/zero of=/dev/sda",
            "dd $'' if=/dev/zero of=/dev/sda",
        ):
            assert is_denied(cmd) is not None, f"empty word escaped the rule: {cmd!r}"

    def test_the_tolerant_rule_family_does_not_regress(self):
        """``chmod`` is denied by a rule that tolerates the extra separator, so it
        is the control that proves the empty-word handling did not trade one
        family for another."""
        for cmd in (
            "chmod 777 /etc/passwd",
            'chmod "" 777 /etc/passwd',
            "mkfs.ext4 /dev/sda1",
            'mkfs.ext4 "" /dev/sda1',
        ):
            assert is_denied(cmd) is not None, cmd

    def test_a_nested_payload_carrying_the_word_is_denied(self):
        """The word is available at both levels: in the wrapper's own argv and
        inside the ``-c`` script, whose payload gets its own view."""
        for cmd in (
            'bash "" -c \'dd "if=/dev/zero" of=/dev/sda\'',
            "bash -c 'dd \"\" if=/dev/zero of=/dev/sda'",
            'bash "" -c \'dd "" if=/dev/zero of=/dev/sda\'',
        ):
            assert is_denied(cmd) is not None, f"nested empty word escaped: {cmd!r}"

    def test_the_tokenizer_still_reports_the_element(self):
        """The empty word is handled in the RECOGNIZER, not the lexer.  ``_shell_tokens`` is
        documented as argv the way a POSIX shell hands it over, and an
        empty-quoted word really is an element of that argv -- so it stays, and
        the ~19 path-normalizer consumers see unchanged tokens.  Only the view
        gains a render without it.
        """
        from kiro_crew import security

        assert security._shell_tokens('rm -rf "" /home/x') == ["rm", "-rf", "", "/home/x"]
        assert security.normalize_shell_command('rm -rf "" /home/x') == [
            "rm",
            "-rf",
            "",
            "/home/x",
        ]
        # Three views: raw, the plain join (unchanged, double-spaced), and the
        # empty-elided join APPENDED beside it -- never instead of it.
        assert security._deny_segment_views('rm -rf "" /home/x') == (
            'rm -rf "" /home/x',
            "rm -rf  /home/x",
            "rm -rf /home/x",
        )

    def test_the_elided_view_is_added_and_never_substituted(self):
        """A rule that REQUIRES an intervening token matches the double-spaced
        view, so replacing that view instead of adding beside it would REMOVE an
        existing denial.

        With a custom rule ``rm -rf .* ./data``, the spelling
        ``r""m -rf "" ./data`` is refused only through the plain join; with the
        plain join dropped it is allowed -- the rule matches neither the elided
        view nor the command's canonical spelling ``rm -rf ./data``, which that
        rule never covered.  The ``r""m`` spelling is what isolates it: the
        simpler ``rm -rf "" ./data`` keeps a raw-view match, because ``.*``
        happily spans the quote characters.

        This is the concrete reason ``_deny_segment_views`` only ever ADDS views.
        """
        custom = ["rm -rf .* ./data"]
        for cmd in (
            "rm -rf -v ./data",  # the shape the rule is authored for
            'rm -rf "" ./data',
            'r""m -rf "" ./data',  # the isolating spelling
            "rm -rf '' ./data",
        ):
            assert (
                is_denied(cmd, denied_regexes=custom) is not None
            ), f"an existing denial was lost: {cmd!r}"
        # The canonical spelling was never covered by that rule, before or after,
        # which is what makes the rows above denials to PRESERVE rather than a
        # coverage claim this change should be making.
        assert is_denied("rm -rf ./data", denied_regexes=custom) is None

    def test_benign_commands_with_an_empty_word_stay_allowed(self):
        """Eliding a zero-width element renders what the command does; it must not
        invent a match for a command that does nothing destructive."""
        for cmd in (
            'echo "" hello',
            "printf '%s' ''",
            'git "" status',
            'grep "" notes.txt',
            'test "" = ""',
            'ls "" -la',
        ):
            assert is_denied(cmd) is None, f"benign empty word over-blocked: {cmd!r}"

    EMPTY_WORDS = ('""', "''", "$''", '$""', "\"\"''", '""""')

    # Single-segment commands, one per rule shape.  ``git push origin main`` is
    # here for the VIEW property; its deny property is enforced by the argv
    # floor rather than the tiers -- see
    # ``test_the_git_publish_detector_skips_an_empty_word``.
    PROPERTY_BASES = (
        "rm -rf /home/x",
        "dd if=/dev/zero of=/dev/sda",
        "chmod 777 /etc/passwd",
        "git push origin main",
        "ls -la",
        "cat /etc/passwd",
    )

    def _empty_word_variants(self, base: str):
        """*base* with each empty-word spelling inserted at every argument boundary."""
        words = base.split(" ")
        for word in self.EMPTY_WORDS:
            for at in range(len(words) + 1):
                yield at, word, " ".join(words[:at] + [word] + words[at:])

    def test_inserting_an_empty_word_at_any_boundary_changes_no_view(self):
        """The mechanical catch for the whole empty-word class, expressed
        against the VIEW instead of rule by rule.

        The harvest proposed asserting that inserting ``""`` at each argument
        boundary of every catalog command still denies.  Stated against the view
        the property is stronger and rule-INDEPENDENT: if the normalized view of
        the command with an empty word inserted is IDENTICAL to the view without
        it, then no rule matched against that view -- including one a per-family
        list would omit, and one added later -- can decide the two differently.  A
        per-rule sweep would also need a command synthesized from each of the ~140
        rule regexes, which is not mechanical; this is.
        """
        from kiro_crew import security

        for base in self.PROPERTY_BASES:
            expected = security._deny_segment_views(base)[-1]
            for at, word, variant in self._empty_word_variants(base):
                views = security._deny_segment_views(variant)
                assert views[-1] == expected, (
                    f"{word} at position {at} of {base!r} changed the view: "
                    f"{views[-1]!r} != {expected!r}"
                )

    def test_the_deny_decision_follows_the_view_for_every_boundary(self):
        """The view property above, carried through to the decision the gate
        actually returns.  The non-git bases are decided by the deny TIERS;
        the git base is enforced by the argv floor, swept here too because it
        has no empty-word gap (an interposed word denies at
        every boundary -- via the protected-branch rule where the parse holds,
        via the ungated anti-obfuscation branch where it does not)."""
        from kiro_crew import security

        for base in self.PROPERTY_BASES:
            expected_denied = security.is_denied(base) is not None
            for _at, _word, variant in self._empty_word_variants(base):
                assert (
                    security.is_denied(variant) is not None
                ) == expected_denied, f"{variant!r} decided differently from {base!r}"

    def test_the_git_publish_detector_skips_an_empty_word(self):
        """The empty-word gap is closed -- this is the flipped form of the
        ``test_the_git_publish_detector_is_a_separate_pre_existing_gap`` pin,
        and this test pins the closure.

        Every git-publish rule is stripped from the regex tier and enforced
        solely by an argv floor (``_git_publish_floor_tags``).  Its entry
        detector's raw-text pass still requires the program and subcommand
        adjacent, but the normalizer second pass
        (``_is_git_push_via_normalizer``) skips empty and whitespace-only
        argv words when seeking the subcommand, so an interposed empty word does
        not hide the push from the floor.  The widening is deliberate
        fail-closed OVER-detection: git does not ignore a zero-width word (it
        takes it as its command name and exits), so a spelling this newly
        reaches either fails to run a push at all or was already reached in
        its adjacent spelling -- no runnable push gains an escape.  For the
        newly-reached spellings the floor's ``_git_push_args`` parse fails on
        the interposed word, so the deny comes from the UNGATED
        anti-obfuscation branch (``_GIT_PUBLISH_UNGATED``), not from
        ``_is_push_to_protected_branch`` -- the right treatment for a spelling
        git itself cannot run.
        """
        from kiro_crew import security

        assert is_denied("git push origin main") is not None, (
            "the protected-branch floor no longer fires on the plain spelling -- this "
            "pin is measuring nothing"
        )
        # Every empty-word spelling the view property enumerates, interposed
        # at the exact boundary the entry detector would bail on, plus the
        # whitespace-only shapes.
        base = "git push origin main".split(" ")
        for word in self.EMPTY_WORDS + ('" "', "$'\\t'"):
            cmd = " ".join([base[0], word] + base[1:])
            assert (
                is_denied(cmd) is not None
            ), f"an interposed word escaped the git-publish floor: {cmd!r}"
        # The DISCRIMINATING pin for the seek-loop closure is the predicate
        # itself: the end-to-end deny above can also arrive via the ungated
        # parse-failure branch, and the flag spellings below already match the
        # pass-1 raw regex, so only a direct call proves the normalizer seek
        # steps over the empty word (and, for the flag rows, that a global
        # flag still consumes its empty argument without drifting off the
        # subcommand position).
        for cmd in (
            'git "" push origin main',
            "git '' -c x=y push origin main",
            "git -c '' push origin main",
        ):
            assert (
                security._is_git_push_via_normalizer(cmd) is True
            ), f"the normalizer seek did not resolve the subcommand: {cmd!r}"
        # ...and the end-to-end deny for the flag spellings holds too.
        for cmd in (
            "git -c '' push origin main",
            "git -C '' push origin main",
            "git '' -c x=y push origin main",
        ):
            assert is_denied(cmd) is not None, cmd
        # A post-subcommand empty word was always tolerated (argv parsing has
        # begun by then) and stays unchanged.
        assert is_denied('git push "" origin main') is not None
        # The subcommand-position requirement is intact: ``stash push`` with an
        # interposed empty word is still not a publish.
        assert is_denied('git "" stash push') is None

    def test_a_whitespace_only_word_is_elided_from_an_added_view(self):
        """A quoted WHITESPACE-ONLY word does not hide a command-shape rule.

        ``rm -rf " " /home/x`` runs the recursive delete with one extra operand
        (a file named by a single space), but the single-space join renders that
        operand as extra separators and the rule's text is not a substring of the
        plain view or the empty-elided one.  A FOURTH view drops whitespace-only
        tokens.  It is appended beside the others, never substituted, so it can
        only add a match: a view whose match an exception excuses moves on to the
        next pattern, and any other view that matches still denies.

        The view is one operand short of the argv that runs, so it is a render
        to match deny rules against, not a claim about which files are named.
        Whitespace-collapsing the joined line instead is unsound: it would merge a
        two-word filename into two operands, so a token that carries any
        non-whitespace character is kept whole -- the last assertion pins that.
        """
        from kiro_crew import security

        for cmd in (
            'rm -rf " " /home/x',
            "rm -rf $'\\t' /home/x",
            "rm -rf ' ' $'\\t' /home/x",
        ):
            assert (
                is_denied(cmd) is not None
            ), f"a whitespace-only word escaped the deny view: {cmd!r}"
        # The added view is appended after the plain and empty-elided renders.
        views = security._deny_segment_views('rm -rf " " /home/x')
        assert views[-1] == "rm -rf /home/x"
        assert "rm -rf   /home/x" in views
        # A command with no whitespace-only word gains no view.
        assert security._deny_segment_views("ls -la /tmp") == ("ls -la /tmp",)
        # ...and the two-word filename that makes a whitespace collapse unsound.
        assert security._deny_segment_views('rm -rf "a b"')[-1] == "rm -rf a b"

    def test_the_self_protection_floor_was_never_fooled(self):
        """The argv-structural floor matches token frames, not a rendered line, so
        the empty word never reached it -- pinned so a later refactor cannot move
        those floors onto the rendered view and inherit this class of escape.

        The frame keeps the empty element as the operand it is, and that is the
        RIGHT reading: ``kirocrew "" restart`` hands argparse an empty subcommand,
        which it rejects (``invalid choice: ''``), so nothing restarts and the
        command is allowed. The earlier form of this test asserted a denial for
        that spelling -- a denial the deleted ``.*kiro.?crew ... restart.*`` row
        produced from the elided VIEW, not the floor, so the floor's own verdict
        was never being tested. Only the bare spelling is a restart.
        """
        prog = "kiro" + "crew"
        assert is_denied(f"{prog} restart") is not None
        for cmd in (f'{prog} "" restart', f'{prog} -v "" restart'):
            assert not security._is_self_restart(cmd), cmd
            assert is_denied(cmd) is None, cmd
