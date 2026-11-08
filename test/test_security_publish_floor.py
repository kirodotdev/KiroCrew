"""Tests for kiro_crew.security: the git-publish floor over nested shell payloads.

The floor descends into nested and glued ``-c`` payloads and reads ``(`` / ``)`` as shell
operators, so no wrapper or subshell hides a push to a protected branch.
"""

from __future__ import annotations


class TestPublishFloorNestedPayloads:
    """The publish floor must descend into nested shell payloads.

    Every git-publish rule is stripped from the regex tier, so
    ``_is_git_publish`` is the SOLE enforcement for pushes. It matched only the
    top-level text, so a single wrapper was a complete bypass -- while the
    self-protection floor beside it was already immune because it re-tokenizes
    payloads through the same walk. These pin that the two floors now share it.
    """

    WRAPPED_PROTECTED = (
        "bash -c 'git push origin main'",
        "sh -c 'git push origin main'",
        "bash -lc 'git push --force origin main'",
        "bash -c -- 'git push origin mainline'",
        "eval 'git push origin main'",
        "bash <<< 'git push origin main'",
        "bash -c 'bash -c \"git push origin main\"'",  # nested two deep
        "$SHELL -c 'git push origin mainline'",
        "bash -c 'git push --mirror origin'",
        "echo 'git push origin main' | bash",
    )

    def test_wrapped_protected_push_denied(self) -> None:
        from kiro_crew.security import is_denied

        for cmd in self.WRAPPED_PROTECTED:
            assert is_denied(cmd) is not None, cmd

    def test_glued_command_flag_spelling_denied(self) -> None:
        """``-c'<push>'`` (no space) is one token; the payload must still surface.

        The bare-flag pattern rejects a token carrying the payload's own
        characters, so the glued spelling was never yielded and the publish
        floor -- whose ONLY enforcement is this walk -- never judged it.
        """
        from kiro_crew.security import is_denied

        for cmd in (
            "bash -c'git push origin main'",
            'sh -c"git push origin main"',
            "bash -lc'git push --force origin main'",
            "bash -ec'git push origin mainline'",
        ):
            assert is_denied(cmd) is not None, cmd

    def test_end_of_options_terminator_does_not_hide_the_payload(self) -> None:
        """``--`` ends option parsing, so the script is the token AFTER it.

        ``eval -- '<script>'`` yielded the literal ``--`` as the payload, so the
        real script was never walked and the push executed. The ``-c`` branch
        already skipped the terminator; the verb branch did not.
        """
        from kiro_crew.security import _shell_payload_sources, is_denied

        assert "git push origin main" in _shell_payload_sources("eval -- 'git push origin main'")
        for cmd in (
            "eval -- 'git push origin main'",
            "eval -- -- 'git push origin mainline'",
            "bash -c -- 'git push origin main'",
        ):
            assert is_denied(cmd) is not None, cmd

    def test_eval_concatenates_its_arguments_into_one_command(self) -> None:
        """``eval a b c`` evaluates ``a b c``, so no single argument looks like one.

        Taking only the first argument let the publish through: the walk handed
        the hooks the bare program name and the verb sat in the next word, which
        no check ever saw. Splitting across MORE words was already caught, because
        each word then appears as its own token -- the gap was specifically the
        program alone in one word and the whole verb-and-args tail glued into the
        next.
        """
        from kiro_crew.security import _shell_payload_sources, is_denied

        assert "git push origin main" in _shell_payload_sources("eval 'git' 'push origin main'")
        for cmd in (
            "eval 'git' 'push origin main'",
            "eval -- 'git' 'push origin main'",
            "eval 'git' 'push --force origin mainline'",
            "eval 'git push' 'origin main'",
        ):
            assert is_denied(cmd) is not None, cmd

    def test_eval_join_does_not_over_block_ordinary_multi_word_eval(self) -> None:
        from kiro_crew.security import is_denied

        for cmd in (
            "eval 'ls' '-la'",
            "eval 'echo' 'hello world'",
            "eval 'git' 'status'",
            "eval 'git' 'push origin my-feature'",
        ):
            assert is_denied(cmd) is None, cmd

    def test_a_wrapped_feature_branch_push_is_not_blocked(self) -> None:
        """The over-block: ordinary work refused along with the protected case.

        Admitting ``(`` as a leading separator makes the OUTER wrapper line
        match the publish detector, because the ``(`` sits right after the
        wrapper's quote. That line is not itself a push -- the push text lives
        inside one quoted argument -- so no ``git`` token is there to parse, and
        the "detected but unparseable" rule denied it. That rule is for
        obfuscation, which a quoted payload is not, so a FEATURE-branch push
        inside a subshell inside a wrapper was refused.
        """
        from kiro_crew.security import is_denied

        for cmd in (
            "bash -c '(git push origin my-feature)'",
            'bash -c "(git push origin my-feature)"',
            "bash -c '(cd /tmp && git push origin my-feature)'",
            "bash -c \"(git push origin 'release/x')\"",
            "sh -c '(git push origin fix/some-branch)'",
        ):
            assert is_denied(cmd) is None, cmd

    def test_the_wrapped_protected_push_is_still_denied(self) -> None:
        """The deferral must not cost the denial it exists alongside.

        These need the payload descent AND the quote-aware operator cut
        together: the ref is quoted inside a subshell inside a wrapper.
        """
        from kiro_crew.security import is_denied

        for cmd in (
            "bash -c '(git push origin main)'",
            "bash -c \"(git push origin 'main')\"",
            "sh -c \"(cd /tmp; git push origin 'main')\"",
            "bash -c \"(git push --force origin 'mainline')\"",
            "eval \"(git push origin 'mainline')\"",
        ):
            assert is_denied(cmd) is not None, cmd

    def test_a_verb_named_argument_does_not_buy_a_deferral(self) -> None:
        """The deferral must key on a payload that is itself a publish.

        Asking only whether a payload EXISTS was a bypass. A remote or refspec
        that happens to share a name with a shell verb makes the payload walk
        report a payload, and QUOTING the program defeats the ``git`` anchor so
        the args come back None. Together those two let a protected-branch
        publish through: nothing downstream ever judged it, because the payload
        the outer line deferred to was the bare word ``main``, which is not a
        publish and answers nothing.
        """
        from kiro_crew.security import is_denied

        for cmd in (
            '"git" push eval main',
            "'git' push eval main",
            '"git" push source main',
            '"git" push . main',
            '"git" push origin main',
            "git push eval main",
            "git push source main",
            "git push origin eval main",
        ):
            assert is_denied(cmd) is not None, cmd

    def test_the_publish_floor_returns_a_decision_when_the_walk_raises(self) -> None:
        """The gate must DECIDE, never raise.

        The floor's payload enumeration ran unguarded, so a helper that exploded
        escaped ``is_denied`` and the PreToolUse gate crashed instead of denying.
        On failure it degrades to the top-level reading -- exactly what this
        floor checked before it learned to descend -- so a broken walk costs the
        nested coverage and nothing else.
        """
        import kiro_crew.security as sec

        def boom(*_a: object, **_k: object) -> object:
            raise RuntimeError("payload walk exploded")

        original = sec._nested_shell_payloads
        try:
            sec._nested_shell_payloads = boom  # type: ignore[assignment]
            # Decides rather than raising, and the top-level reading still holds.
            assert sec.is_denied("git push origin main") is not None
            assert sec.is_denied("rm -rf /") is not None
            assert sec.is_denied("git push origin my-feature") is None
        finally:
            sec._nested_shell_payloads = original  # type: ignore[assignment]

    def test_an_operator_in_an_executable_path_is_not_a_shell_operator(self) -> None:
        """Punctuation inside an already-tokenized word belongs to the word.

        The normalizer has tokenized and dequoted before this detector runs, so
        replacing each token with its operator-cut form truncated a legal
        executable path (``/opt/my(dir)/git`` -> ``/opt/my``, whose basename is
        not ``git``). Detection was NARROWED and a protected push through such a
        path went from denied to allowed. Both spellings are consulted now, so
        the widen-only property actually holds.
        """
        from kiro_crew.security import is_denied

        for cmd in (
            '"/opt/my(dir)/git" push origin main',
            "'/opt/my(dir)/git' push origin main",
            '"/opt/my(dir)/git" push origin mainline',
            '"/opt/a(b)/git" push --force origin main',
            "/usr/bin/git push origin main",
            '"/usr/bin/git" push origin main',
        ):
            assert is_denied(cmd) is not None, cmd

        # The glued-operator spellings the cut exists for still resolve.
        for cmd in ("(git push origin main)", "(git push origin 'main')"):
            assert is_denied(cmd) is not None, cmd

    def test_obfuscation_with_no_payload_still_fails_closed(self) -> None:
        """The deferral is NOT a general escape hatch.

        The outer reading defers only when a nested payload exists to defer TO.
        Glue-evasion carries no payload, so it must still be denied on the spot.
        """
        from kiro_crew.security import is_denied

        for cmd in (
            "git$(echo ' ')push origin main",
            "git`echo ' '`push origin main",
            "git push origin ma$(echo)in",
            "git push",
            "git push --mirror origin",
        ):
            assert is_denied(cmd) is not None, cmd

    def test_the_eval_join_stays_linear(self) -> None:
        """A join is O(N), so one per verb token would be quadratic.

        The nested-payload walk was deliberately made linear and is pinned that
        way, but those shapes use shell-program tokens only, so this path is not
        covered there. Bounding the join to once per walk keeps it linear, and
        one is enough because it runs to the END of the token list and therefore
        already spans every later verb's own suffix.

        Growth is measured as the total characters of payload the walk produces,
        not as wall-clock time. A join's cost is the length of the string it
        builds, so the character total IS the join work, and it is a pure
        function of the input: an 8x size gap reads as exactly 8x for a linear
        walk and 64x for one that joins once per verb, with nothing to tune. The
        wall-clock form of this assertion was widened once and still flipped on a
        loaded host, because the small sample is a few milliseconds and one
        preemption during the large one breaches any ratio a regression would
        also breach. Interpreter call counts do not work either: ``str.join`` is
        one C call whatever its length, so an unbounded join reads as linear
        there. The exact, size-free half of the property is pinned by
        ``test_only_one_joined_payload_is_produced_per_walk`` (one join per call)
        and ``test_a_join_produced_frame_does_not_join_again`` (no join chain).
        """
        from kiro_crew.security import _nested_shell_payloads

        def payload_chars(n: int) -> int:
            tokens = ["eval", "a", "b"] * n
            return sum(len(payload) for payload in _nested_shell_payloads(list(tokens)))

        small, large = payload_chars(2000), payload_chars(16000)
        # 16 sits between the linear reading (8x) and the quadratic one (64x).
        assert large < small * 16, f"{small} -> {large} payload chars looks super-linear"

    def test_only_one_joined_payload_is_produced_per_walk(self) -> None:
        """The bound above is what keeps it linear, so pin the bound itself."""
        from kiro_crew.security import _nested_shell_payloads

        tokens = ["eval", "git", "push origin main", "eval", "x", "y"]
        payloads = _nested_shell_payloads(list(tokens))
        joined = [p for p in payloads if " " in p and p.count(" ") > 1]
        assert len(joined) == 1, payloads
        # The one join reaches the end, so the later verb's suffix is inside it.
        assert joined[0].endswith("x y"), joined
        assert "push origin main" in joined[0], joined

    def test_a_join_produced_frame_does_not_join_again(self) -> None:
        """The join is once per FRAME; the chain it can build is the real cost.

        A joined payload is strictly shorter than its parent, so it becomes a frame
        of its own -- and if that frame joins too, both walks build a chain of
        shrinking suffixes, N frames each costing an O(N) lex and an O(N) join.
        Measured on ``"eval " * 1280``: 65 s, growing ~5x per doubling, against
        0.13 s before the join existed. Frame counts are pinned instead of timings
        because they are exact: they do not grow with N at all.
        """
        from kiro_crew.security import _deny_segment_views, _shell_payload_walk

        counts = {
            n: (len(_shell_payload_walk("eval " * n)), len(_deny_segment_views("eval " * n)))
            for n in (8, 16, 64, 256)
        }
        assert len(set(counts.values())) == 1, counts
        assert all(walk <= 4 and views <= 5 for walk, views in counts.values()), counts

    def test_the_join_still_fuses_a_split_publish_at_any_depth(self) -> None:
        """Declining the SECOND join costs no detection.

        The join fuses already-dequoted words in one step, so ``eval eval 'git'
        'push origin main'`` is fused to ``git push origin main`` by the first join
        and the chain only re-derived suffixes of an answer already in hand.
        """
        from kiro_crew.security import is_denied

        for cmd in (
            "eval 'git' 'push origin main'",
            "eval eval 'git' 'push origin main'",
            "eval " * 8 + "'git' 'push origin main'",
            "eval " * 512 + "'git' 'push origin main'",
            "bash -c \"eval eval 'git' 'push origin main'\"",
            "$(eval 'git' 'push origin main')",
            "cat <(eval 'git' 'push origin main')",
            # two sibling frames, each needing its OWN join
            "bash -c \"eval 'git' 'push origin feat'\" ; "
            "bash -c \"eval 'git' 'push origin main'\"",
        ):
            assert is_denied(cmd) is not None, cmd

        for cmd in (
            "eval 'git' 'push origin my-feature'",
            "eval eval 'git' 'push origin my-feature'",
            "eval 'echo' 'hello world'",
        ):
            assert is_denied(cmd) is None, cmd

    def test_source_arguments_are_not_joined(self) -> None:
        """``source``/``.`` take a FILE; the rest are positional parameters.

        Joining them would invent a command line bash never runs, so the
        concatenation is scoped to ``eval`` alone.
        """
        from kiro_crew.security import _nested_shell_payloads, normalize_shell_command

        for cmd in ("source setup.sh arg1 arg2", ". setup.sh arg1 arg2"):
            payloads = _nested_shell_payloads(normalize_shell_command(cmd))
            assert payloads == ["setup.sh"], (cmd, payloads)

    def test_prefix_forms_of_a_real_push_still_denied(self) -> None:
        """Guards against narrowing detection to fix the ``echo`` false positive.

        Requiring ``git`` to sit in ``_argv_programs`` command position was tried
        and silently broke all five of these, so the walk deliberately still
        scans every token.
        """
        from kiro_crew.security import is_denied

        for cmd in (
            "/usr/bin/git push origin main",
            "env FOO=1 git push origin main",
            "sudo git push origin main",
            "nohup git push origin main",
            "command git push origin main",
            "bash -c 'env X=1 git push origin mainline'",
        ):
            assert is_denied(cmd) is not None, cmd

    def test_wrapped_feature_push_still_allowed(self) -> None:
        from kiro_crew.security import is_denied

        # The floor decides protected-vs-feature, so widening DETECTION must not
        # turn ordinary work into a denial.
        for cmd in (
            "bash -c 'git push origin my-feature'",
            "sh -c 'git push origin fix/thing'",
        ):
            assert is_denied(cmd) is None, cmd

    def test_wrapped_benign_not_overblocked(self) -> None:
        from kiro_crew.security import is_denied

        for cmd in (
            "bash -c 'echo remember to push later'",
            "bash -c 'git fetch origin main'",
            "bash -c 'ls -la'",
            "git stash push -m wip",
        ):
            assert is_denied(cmd) is None, cmd

    def test_self_protection_floor_shares_the_walk(self) -> None:
        from kiro_crew.security import is_denied

        # Same walk now feeds both floors; the self-protection side must not
        # regress when the publish side starts consuming it.
        for cmd in (
            "bash -c 'kirocrew token'",
            "bash -c 'kirocrew restart'",
            "cat <(kirocrew token)",
        ):
            assert is_denied(cmd) is not None, cmd

    def test_payload_sources_and_frames_agree(self) -> None:
        from kiro_crew.security import _self_token_frames, _shell_payload_sources

        # The two views are projections of ONE walk, so they must stay the same
        # length -- a drift here is the class of bug this refactor removes.
        cmd = "bash -c 'git push origin main'"
        assert len(_shell_payload_sources(cmd)) == len(_self_token_frames(cmd))
        assert cmd in _shell_payload_sources(cmd)
        assert "git push origin main" in _shell_payload_sources(cmd)


class TestGluedShellCommandPayloadExtraction:
    """A payload GLUED to a ``-c`` short-option cluster is extracted.

    ``sh -c'rg . /fenced/root'`` reaches the walk as ONE token
    (``-crg . /fenced/root``) once shlex strips the quotes.
    ``_SHELL_COMMAND_FLAG_RE`` anchors the whole token as a bare flag cluster, so
    a token carrying the payload's own characters was rejected -- and a payload
    the extractor does not return is a command NONE of its consumers look
    inside, the self-protection floor included.  The companion pattern
    ``_SHELL_COMMAND_GLUED_RE`` captures the glued remainder instead of
    weakening the flag pattern where it is used for pure flag detection.
    """

    def test_every_glued_spelling_yields_the_spaced_payload(self) -> None:
        """Glued single-quoted, double-quoted, and clustered spellings agree."""
        from kiro_crew.security import _nested_shell_payloads, _shell_tokens

        spaced = _nested_shell_payloads(_shell_tokens("sh -c 'rg . /fenced/root'"))
        assert spaced == ["rg . /fenced/root"], spaced
        for cmd in (
            "sh -c'rg . /fenced/root'",  # glued single-quoted
            'sh -c"rg . /fenced/root"',  # glued double-quoted
            "sh -ec'rg . /fenced/root'",  # letters BEFORE the c in the cluster
            "sh -xc'rg . /fenced/root'",
        ):
            payloads = _nested_shell_payloads(_shell_tokens(cmd))
            assert payloads == spaced, (cmd, payloads)

    def test_glued_unquoted_payload_is_extracted(self) -> None:
        """No quotes at all: ``-cwhoami`` runs ``whoami`` in a real shell."""
        from kiro_crew.security import _nested_shell_payloads, _shell_tokens

        payloads = _nested_shell_payloads(_shell_tokens("bash -cwhoami"))
        assert "whoami" in payloads, payloads

    def test_bare_cluster_is_not_read_as_glued(self) -> None:
        """Negative: ``-lc`` and ``-c`` carry no payload of their own.

        The script is the NEXT token, exactly as before -- the glued reading must
        not invent a second payload out of a bare flag cluster.
        """
        from kiro_crew.security import _nested_shell_payloads, _shell_tokens

        payloads = _nested_shell_payloads(_shell_tokens("bash -lc 'git status'"))
        assert payloads == ["git status"], payloads
        assert _nested_shell_payloads(_shell_tokens("bash -c")) == []

    def test_all_alpha_cluster_yields_both_readings(self) -> None:
        """``-ecfoo`` is ambiguous post-tokenization, so BOTH readings surface.

        It matches the bare-flag pattern (the next token is the script, the
        reading this extractor always had) AND a real shell ends option parsing
        at the ``c`` and runs ``foo``.  Picking one interpretation would make the
        other a bypass; extraction over-approximates instead, which this module
        documents as the safe direction.
        """
        from kiro_crew.security import _nested_shell_payloads, _shell_tokens

        payloads = _nested_shell_payloads(_shell_tokens("bash -ecfoo bar"))
        assert "foo" in payloads, payloads
        assert "bar" in payloads, payloads

    def test_a_glued_decoy_does_not_eat_a_later_spaced_payload(self) -> None:
        """The two spellings are scanned independently, so both yield.

        Folding the glued spelling into the shared stop table would let a glued
        decoy consume the stop through which a later spaced ``-c``'s payload was
        found, turning the independent scan itself into a bypass.
        """
        from kiro_crew.security import _nested_shell_payloads, _shell_tokens

        payloads = _nested_shell_payloads(_shell_tokens("bash -cx.sh -c 'rg . /fenced/root'"))
        assert "x.sh" in payloads, payloads
        assert "rg . /fenced/root" in payloads, payloads

    def test_a_herestring_does_not_eat_a_later_command_flag_payload(self) -> None:
        """The herestring stop is independent of the ``-c`` stop for the same
        reason: sharing one table let ``bash <<<'x' -c '<script>'`` yield only
        ``x`` while a real shell runs the script."""
        from kiro_crew.security import _nested_shell_payloads, _shell_tokens

        payloads = _nested_shell_payloads(_shell_tokens("bash <<<'x' -c 'rg . /fenced/root'"))
        assert "x" in payloads, payloads
        assert "rg . /fenced/root" in payloads, payloads

    def test_uppercase_cluster_letters_still_carry_the_payload(self) -> None:
        """``-C`` (noclobber) clusters like any other flag, and the alt pass
        feeds case-PRESERVING tokens -- a lowercase-only class dropped these."""
        from kiro_crew.security import _nested_shell_payloads, _shell_tokens

        for cmd in (
            "bash -Cc'rg . /fenced/root'",
            "bash -Cc 'rg . /fenced/root'",
        ):
            payloads = _nested_shell_payloads(_shell_tokens(cmd))
            assert "rg . /fenced/root" in payloads, (cmd, payloads)

    def test_case_folded_cluster_splits_are_all_examined(self) -> None:
        """Which ``c`` took the argument is unrecoverable after the case fold.

        The deny tiers lowercase input before the walk, so ``-Cc'<script>'``
        (``-C`` noclobber + ``-c`` script, a real zsh/ksh spelling) folds to
        ``-cc<script>`` and the first-``c`` split reads the payload as
        ``c<script>`` -- one junk letter hid a protected push from the publish
        floor, and the attacker can also write the folded spelling directly
        (found by the GPT 5.6 CI lane).  Every plausible split is yielded
        instead: the run's last ``c`` (all flags) and second-to-last (a payload
        whose program starts with one ``c``, like ``cat``).
        """
        from kiro_crew.security import (
            _nested_shell_payloads,
            _shell_c_carrier_payloads,
            _shell_tokens,
            is_denied,
        )

        for cmd in (
            "zsh -Cc'git push origin main'",
            "bash -Cc'git push origin main'",
            "zsh -cc'git push origin main'",  # folded spelling written directly
            "bash -Cc'git push --force origin main'",
        ):
            assert is_denied(cmd) is not None, cmd
        # The correct boundary is among the yielded candidates.
        payloads = _nested_shell_payloads(_shell_tokens("zsh -cc'git push origin main'"))
        assert "git push origin main" in payloads, payloads
        # A payload whose program name itself starts with ``c``.
        assert "cat /fenced/file" in _shell_c_carrier_payloads("-cccat /fenced/file")
        # A ``c`` past the first non-letter belongs to the payload's own text:
        # splitting there would shred the payload, so it is not a candidate.
        assert _shell_c_carrier_payloads("-crg . /fenced/root") == ["rg . /fenced/root"]
        # Split positions are bounded to the window: an alternating-``c``
        # cluster of any length yields a bounded candidate set instead of a
        # quadratic one (a ~3 KB such token outlived the loop watchdog), and
        # the bound is not a padding bypass -- the true split sits within a
        # first-word length of the region's end, and padding only adds fake
        # splits farther out.
        flooded = _shell_c_carrier_payloads("-" + "ac" * 1600 + "c'git push origin main'")
        assert len(flooded) <= 70, len(flooded)
        # Feature-branch pushes and benign scripts stay allowed.
        assert is_denied("bash -Cc'git push origin my-feature'") is None
        assert is_denied("bash -cc'ls -la'") is None

    def test_an_uppercase_cluster_does_not_eat_the_command_flag_stop(self) -> None:
        """The flag pattern stays lowercase-only ON PURPOSE.

        Widening it to ``[A-Za-z]`` made ``-Cc`` the first flag stop, which ate
        the stop through which a following ``--command``'s payload was found --
        the one old-stop class neither the glued table nor the sweep reaches.
        Uppercase clusters are covered by the sweep and the glued pattern
        instead, so BOTH payloads surface for the CASE-PRESERVING callers (the
        alt-traversal pass, pinned here by calling the extractor directly).
        The deny tiers lowercase first, where ``-Cc`` folds to ``-cc`` and the
        ``--command`` residual remains -- pre-existing there, and out of this
        pattern's reach.
        """
        from kiro_crew.security import _nested_shell_payloads, _shell_tokens

        payloads = _nested_shell_payloads(_shell_tokens("bash -Cc --command 'rg . /fenced/root'"))
        assert "rg . /fenced/root" in payloads, payloads

    def test_every_carrier_is_swept_not_only_the_first_stop(self) -> None:
        """Each stop table reads ONE token per shell, so a decoy that satisfies
        the same predicate eats the stop through which a later carrier's payload
        was found.  The every-carrier sweep restores what the alt pass's deleted
        local extractor yielded: a payload for EVERY ``-c`` carrier, under the
        loose recognition (any prefix before the first lowercase ``c``).
        """
        from kiro_crew.security import _nested_shell_payloads, _shell_tokens

        # A glued decoy before a glued carrier (both satisfy the glued predicate).
        payloads = _nested_shell_payloads(_shell_tokens("ksh -onoclobber -c'rg . /fenced/root'"))
        assert "rg . /fenced/root" in payloads, payloads
        # A flag decoy before a spaced carrier (both satisfy the flag predicate).
        payloads = _nested_shell_payloads(
            _shell_tokens("bash -Cc benign -c 'git push origin main'")
        )
        assert "git push origin main" in payloads, payloads
        # Two spaced carriers: the second must not collapse into the first.
        payloads = _nested_shell_payloads(_shell_tokens("bash -c 'true' -c 'rg . /fenced/root'"))
        assert "true" in payloads, payloads
        assert "rg . /fenced/root" in payloads, payloads
        # A glued decoy before a glued carrier of the publish floor's payload.
        payloads = _nested_shell_payloads(_shell_tokens("bash -cx.sh -c'git push origin main'"))
        assert "git push origin main" in payloads, payloads

    def test_non_alpha_cluster_prefixes_still_carry_the_payload(self) -> None:
        """The deleted local extractor tolerated ANY prefix before the first
        lowercase ``c`` (``-1c``); the loose sweep preserves that recognition."""
        from kiro_crew.security import _nested_shell_payloads, _shell_tokens

        payloads = _nested_shell_payloads(_shell_tokens("bash -1c 'rg . /fenced/root'"))
        assert "rg . /fenced/root" in payloads, payloads
        payloads = _nested_shell_payloads(_shell_tokens("bash -1c'rg . /fenced/root'"))
        assert "rg . /fenced/root" in payloads, payloads

    def test_many_shells_sharing_one_long_glued_payload_stay_linear(self) -> None:
        """N shell tokens all stop at ONE glued token carrying a length-N payload.

        Extracting at the stop index per shell token copies the same length-N
        substring N times -- O(N^2) time and memory for an O(N)-sized input,
        inside the synchronous permission gate.  The payload is extracted once per TOKEN up front and later
        appends reuse the cached string, so the walk stays linear.  Measured
        across an 8x size gap with a 20x bound, the same methodology the
        eval-join linearity test above documents.
        """
        import time

        from kiro_crew.security import _nested_shell_payloads

        def elapsed(n: int) -> float:
            tokens = ["bash"] * n + ["-c" + "x" * n]
            start = time.perf_counter()
            _nested_shell_payloads(tokens)
            return time.perf_counter() - start

        def best(n: int, samples: int = 3) -> float:
            return min(elapsed(n) for _ in range(samples))

        elapsed(500)
        small, large = best(2000), best(16000)
        # flake-ok: pre-existing best-of-three wall-clock ratio of two spans this test measures; moved verbatim and tracked for a de-flake pass
        assert large < small * 20, f"{small:.4f}s -> {large:.4f}s looks super-linear"
        # No absolute cap, matching the eval-join test above: under the backend
        # jobs' coverage tracing wall time prices line events, not algorithmic
        # cost; the same-run ratio is the regression guard.

    def test_glued_payload_reaches_the_regex_tier_views(self) -> None:
        """Consumer: the deny-view pass judges the glued payload's own text."""
        from kiro_crew.security import is_denied

        spaced = "bash -c 'dd \"if=/dev/zero\" of=/dev/sda'"
        glued = "bash -c'dd \"if=/dev/zero\" of=/dev/sda'"
        assert is_denied(spaced) is not None
        assert is_denied(glued) is not None


class TestGitPublishSubshellGluing:
    """``(`` and ``)`` are shell OPERATORS, so they cannot hide a git push.

    Every git-publish rule is stripped from the regex tier, which makes
    ``_is_git_publish`` the SOLE enforcement for pushes. A paren glued to the
    program (``(git push``) defeated the detector, and a paren glued to the ref
    (``main)``) defeated the protected-name compare -- the latter also emitted a
    SEL ``push_allowed`` event labelled ``feature_branch_push`` for a
    protected-branch force-push.
    """

    GLUED_PROTECTED_PUSHES = (
        "(git push origin main)",
        "((git push origin main))",
        "(cd /tmp; git push origin main)",
        "(cd /tmp && git push origin mainline)",
        "(cd /tmp; git push --force origin mainline)",
        "(true; git push origin head:main)",
        "(git push --mirror origin)",
    )

    def test_glued_subshell_protected_push_denied(self) -> None:
        from kiro_crew.security import is_denied

        for cmd in self.GLUED_PROTECTED_PUSHES:
            assert is_denied(cmd) is not None, cmd

    def test_glued_subshell_push_reaches_protected_branch_check(self) -> None:
        from kiro_crew.security import _is_push_to_protected_branch

        # Not merely denied: the branch check must SEE the protected target, or
        # the allow-audit records a protected push as a feature-branch push.
        for cmd in (
            "(cd /tmp; git push origin main)",
            "(cd /tmp; git push --force origin mainline)",
            "(git push origin mainline)",
        ):
            assert _is_push_to_protected_branch(cmd.lower()) is True, cmd

    GLUED_OPERATOR_PUSHES = (
        "(git push origin main)&",  # trailing background operator
        "(git push origin main);",
        "(git push origin main)|cat",
        "(git push origin mainline)>log",  # operator MID-token, strip cannot reach it
        "(cd /tmp; git push origin main)&",
        "{ git push origin main; }",
        "(git push --force origin mainline)&",
    )

    def test_glued_operator_on_the_ref_is_not_part_of_the_name(self) -> None:
        """bash reads ``main)&`` as the ref ``main`` plus two operators.

        Stripping only parens left ``main)&``, which never equalled ``main``, so a
        protected push was allowed AND audited as a feature-branch push. A
        redirection glued mid-token (``mainline)>log``) is why this cuts at the
        first operator instead of stripping the ends.
        """
        from kiro_crew.security import _is_push_to_protected_branch, is_denied

        for cmd in self.GLUED_OPERATOR_PUSHES:
            assert _is_push_to_protected_branch(cmd.lower()) is True, cmd
            assert is_denied(cmd) is not None, cmd

    def test_cut_at_operator_preserves_a_quoted_ref(self) -> None:
        from kiro_crew.security import _cut_at_operator

        # Unquoted: operators are structure, so cut.
        assert _cut_at_operator("(git") == "git"
        assert _cut_at_operator("main)&") == "main"
        assert _cut_at_operator("mainline)>log") == "mainline"
        assert _cut_at_operator("my-feature") == "my-feature"
        # Quoted: operators are literal text belonging to the ref name.
        assert _cut_at_operator("'(main)'") == "'(main)'"
        assert _cut_at_operator('"(main)"') == '"(main)"'

    def test_quoted_paren_ref_is_not_a_protected_branch(self) -> None:
        from kiro_crew.security import _is_push_to_protected_branch

        # Grouping parens are stripped BEFORE the quotes come off, so a paren the
        # user QUOTED as part of the ref name survives: a branch literally named
        # ``(main)`` is not ``main`` and must stay pushable.
        for cmd in ("git push origin '(main)'", 'git push origin "(main)"'):
            assert _is_push_to_protected_branch(cmd.lower()) is False, cmd

    QUOTED_REF_GLUED_OPERATOR_PUSHES = (
        "(git push origin 'main')",
        '(git push origin "main")',
        "(git push origin 'mainline')",
        '(git push origin "mainline")',
        "(cd /tmp; git push origin 'main')",
        "(git push --force origin 'mainline')",
        "(git push origin 'main')&",
        "{ git push origin 'main'; }",
    )

    def test_quoting_the_ref_does_not_hide_the_glued_operator(self) -> None:
        """A quoted ref can still carry an operator OUTSIDE its quotes.

        Bailing on the mere PRESENCE of a quote reopened the very class this
        cut exists to close: ``(git push origin 'main')`` hands the ref token
        ``'main')``, whose trailing ``)`` is unquoted. Left in place, the ref
        resolved to ``main)``, never equalled ``main``, and the protected push
        was allowed AND audited as ``feature_branch_push``. One quote character
        was the whole bypass.
        """
        from kiro_crew.security import _is_push_to_protected_branch, is_denied

        for cmd in self.QUOTED_REF_GLUED_OPERATOR_PUSHES:
            assert _is_push_to_protected_branch(cmd.lower()) is True, cmd
            assert is_denied(cmd) is not None, cmd

    def test_cut_at_operator_cuts_outside_quotes_only(self) -> None:
        from kiro_crew.security import _cut_at_operator

        # Operator OUTSIDE the quotes is structure -> cut.
        assert _cut_at_operator("'main')") == "'main'"
        assert _cut_at_operator('"main")') == '"main"'
        assert _cut_at_operator("'main')&") == "'main'"
        # Operator INSIDE the quotes is part of the ref name -> keep.
        assert _cut_at_operator("'(main)'") == "'(main)'"
        assert _cut_at_operator("'a;b'") == "'a;b'"
        assert _cut_at_operator("'weird&name'") == "'weird&name'"
        # An unbalanced quote reads the remainder as quoted, so nothing is cut.
        # Safe: bash never runs a command with an unterminated quote.
        assert _cut_at_operator("'main)") == "'main)"

    def test_a_quoted_program_still_anchors_the_push(self) -> None:
        """A quoted ``"git"`` is still the git program to bash.

        Matching the raw token missed it and anchored on a LATER unquoted
        ``git push``, returning only that push's arguments. Appending a benign
        second push therefore hid the first one's protected ref completely and
        turned a fail-closed segment into an allow.
        """
        from kiro_crew.security import _git_push_args, is_denied

        assert _git_push_args('"git" push eval main git push origin my-feature') == [
            "eval",
            "main",
            "git",
            "push",
            "origin",
            "my-feature",
        ]
        for cmd in (
            '"git" push eval main git push origin my-feature',
            "'git' push eval main git push origin my-feature",
            '"git" push origin main git push origin my-feature',
            '"git" push origin main',
            "'git' push origin mainline",
            '"git" push eval main',
        ):
            assert is_denied(cmd) is not None, cmd

    def test_a_nested_feature_push_cannot_vouch_for_the_leading_one(self) -> None:
        """A path-qualified program must anchor, and a redirect ends the args.

        Two halves of one bypass. An exact ``== "git"`` anchor test skipped
        ``/usr/bin/git`` and selected the NESTED ``>(git push origin
        my-feature)`` instead, so the feature branch that process substitution
        pushes answered for the protected push in front of it. Fixing the anchor
        alone left the second half: the nested tokens were still returned as the
        LEADING push's arguments, so a bare ``git push`` -- which must fail
        closed because the current branch may be protected -- inherited a branch
        it never named.
        """
        from kiro_crew.security import _git_push_args, is_denied

        # The anchor is the leading program, whatever its spelling.
        assert _git_push_args("/usr/bin/git push origin main") == ["origin", "main"]
        assert _git_push_args("/opt/my(dir)/git push origin main") == ["origin", "main"]
        # A redirection ends the argument list; the nested command is not a ref.
        assert _git_push_args("git push origin my-feature > >(tee log.txt)") == [
            "origin",
            "my-feature",
        ]
        assert _git_push_args("git push > >(git push origin my-feature)") == []

        for cmd in (
            "/usr/bin/git push origin main > >(git push origin my-feature)",
            "/opt/my(dir)/git push origin main > >(git push origin my-feature)",
            "'/usr/bin/git' push origin main > >(git push origin my-feature)",
            "sudo /usr/bin/git push origin main > >(git push origin my-feature)",
            # bare / under-specified pushes stay fail-closed
            "git push > >(git push origin my-feature)",
            "/usr/bin/git push origin > >(git push origin my-feature)",
            '"git" push > >(git push origin my-feature)',
        ):
            assert is_denied(cmd) is not None, cmd

    def test_a_descriptor_prefixed_redirect_also_ends_the_push_arguments(self) -> None:
        """``2>``, ``&>``, ``1>``, ``{fd}>`` are redirects, not refspecs.

        Testing only the first character of the token recognised ``>`` but not any
        descriptor-prefixed spelling, so the descriptor read as an ordinary refspec
        and the command AFTER the redirect was absorbed as this push's arguments --
        a force push to the current branch answered for by the nested feature push
        it redirected into.
        """
        from kiro_crew.security import _git_push_args, is_denied

        assert _git_push_args("git push --force origin 2> >(cmd)") == ["--force", "origin"]
        assert _git_push_args("git push origin my-feature 2> err.log") == [
            "origin",
            "my-feature",
        ]

        nested = ">(git push origin my-feature)"
        for cmd in (
            f"git push --force origin 2> {nested}",
            f"git push --force origin &> {nested}",
            f"git push --force origin 1> {nested}",
            f"git push --force origin 2>> {nested}",
            f"git push --force origin {{fd}}> {nested}",
            f"git push origin 2> {nested}",
            f"git push 2> {nested}",
            f"/usr/bin/git push --force origin 2> {nested}",
        ):
            assert is_denied(cmd) is not None, cmd

        # The no-over-block half: a redirect of stderr is ordinary tooling.
        for cmd in (
            "git push origin my-feature 2> err.log",
            "git push origin my-feature > out.log 2>&1",
            "git push --force-with-lease origin my-feature 2> err.log",
        ):
            assert is_denied(cmd) is None, cmd

    def test_a_redirect_is_skipped_not_treated_as_the_end_of_the_args(self) -> None:
        """Words AFTER a redirect are still refspecs, and bash keeps them.

        Truncating the argument list at the first redirect dropped every refspec
        behind it, so ``git push origin feature 2>/dev/null main`` was read as a
        feature push and allowed -- while bash removes the redirect and really
        runs ``git push origin feature main``, publishing protected ``main``. The
        redirect construct is stepped over instead: a file target is one word,
        glued or spaced, and a process substitution target is a whole command
        line skipped to its matching ``)``.
        """
        from kiro_crew.security import _git_push_args, is_denied

        # Stepped over, so the trailing refspec survives.
        assert _git_push_args("git push origin feature 2>/dev/null main") == [
            "origin",
            "feature",
            "main",
        ]
        assert _git_push_args("git push origin feature > out main") == [
            "origin",
            "feature",
            "main",
        ]
        # A process substitution is a command, not a refspec: nothing inside it
        # is collected, which is what the boundary exists for.
        assert _git_push_args("git push > >(git push origin my-feature)") == []
        assert _git_push_args("git push origin my-feature > >(tee log.txt)") == [
            "origin",
            "my-feature",
        ]

        for cmd in (
            "git push origin feature 2>/dev/null main",
            "git push origin feature > out main",
            "git push origin feature >out main",
            "git push origin feature 2>&1 main",
            "git push origin feature >> log main",
            "git push origin my-feature 2>/dev/null mainline",
            "git push origin my-feature </dev/null mainline",
            "/usr/bin/git push origin feature 2>/dev/null main",
        ):
            assert is_denied(cmd) is not None, cmd

        for cmd in (
            "git push origin my-feature 2>/dev/null",
            "git push origin my-feature > out.log 2>&1",
            "git push origin my-feature 2> err.log",
            "git push origin my-feature > >(tee log.txt)",
        ):
            assert is_denied(cmd) is None, cmd

    def test_path_qualified_feature_pushes_are_not_over_blocked(self) -> None:
        """The no-over-block half of the same anchor fix.

        These name a feature branch explicitly, so they are ordinary work. They
        were refused only because the reader could not resolve a path-qualified
        or quoted program and fell through to the fail-closed branch.
        """
        from kiro_crew.security import is_denied

        for cmd in (
            "/usr/bin/git push origin my-feature",
            "'/usr/bin/git' push origin feature/x",
            '"git" push -u origin my-feature',
            "git push origin my-feature > >(tee log.txt)",
        ):
            assert is_denied(cmd) is None, cmd

    def test_the_anchor_view_does_not_double_dequote_the_refs(self) -> None:
        """The returned tokens must KEEP their quoting.

        Callers dequote them once more, so stripping quotes here too would read
        a literal ``'(main)'`` ref as the operators ``(``/``)`` around ``main``
        and deny a branch that is legitimately pushable. That is why the
        dequoting is done on a separate anchor view rather than on the tokens.
        """
        from kiro_crew.security import _git_push_args, _is_push_to_protected_branch

        assert _git_push_args("git push origin '(main)'") == ["origin", "'(main)'"]
        assert _is_push_to_protected_branch("git push origin '(main)'") is False

    def test_quoted_operator_ref_names_stay_pushable(self) -> None:
        """The no-over-block half: these are legal, unprotected branch names."""
        from kiro_crew.security import _is_push_to_protected_branch, is_denied

        for cmd in (
            "git push origin '(main)'",
            "(git push origin '(main)')",
            "(git push origin 'release/x')",
            "git push origin 'feature|x'",
            "git push origin 'weird&name'",
            "git push origin 'a;b'",
            "git push origin 'mainly'",
        ):
            assert _is_push_to_protected_branch(cmd.lower()) is False, cmd
            assert is_denied(cmd) is None, cmd

    def test_feature_branch_push_still_allowed_in_subshell(self) -> None:
        # The whole point of the branch check is that ordinary work still runs.
        for cmd in (
            "git push origin my-feature",
            "(cd /tmp; git push origin my-feature)",
            "(git push origin fix/imds-encodings)",
        ):
            from kiro_crew.security import _is_push_to_protected_branch

            assert _is_push_to_protected_branch(cmd.lower()) is False, cmd
