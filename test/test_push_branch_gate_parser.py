"""The parser underneath the git-publish branch gate, pinned layer by layer.

``test_push_branch_gate.py`` pins the gate's answers: the feature-branch allow, the
protected-branch deny, the option and abbreviation readings, and the enforcement
point's audits. This module pins the machinery those answers rest on in
``kiro_crew.security``: every branch of the positional/arity parser as a table, the
contract between the tokenizer and the argument scan, the boundary between a proven
and an unprovable process substitution, the one quote model every push-path reading
walks, and the nested-payload extractor that spans the proven boundary.
"""

from kiro_crew import security


class TestParserBranchTable:
    """Every branch of the positional/arity parser, with why each is correct.

    The issue makes this table an explicit acceptance condition: four
    consecutive findings landed in this one span, each fix narrowing one branch
    while the adjacent one stayed wrong, so the parser's whole behaviour is
    pinned here as data rather than implied by scattered tests. The same table
    appears in the PR body.
    """

    BARE = "git-publish-push-bare"
    SINGLE = "git-publish-push-single-arg"
    MIRROR = "git-publish-push-mirror-all"
    NAME = "git-publish-push-protected-branch-name"
    REF_PATH = "git-publish-push-protected-ref-path"
    AMBIG = "git-publish-push-ambiguous-ref"
    WILD = "git-publish-push-wildcard-refspec"

    # (push args, exact expected tag set, why this is the correct reading)
    TABLE = (
        ("", {BARE}, "no target named — the current branch may be protected"),
        ("origin", {SINGLE}, "remote only, no refspec — same current-branch risk, its own row"),
        ("origin feature-x", set(), "explicit non-protected target — the allowed shape"),
        ("origin main", {NAME}, "explicit protected target by bare name"),
        ("origin refs/heads/main", {REF_PATH}, "protected target by ref path — separate row"),
        ("origin head", {AMBIG}, "symbolic ref resolves at runtime — unverifiable"),
        ("origin refs/heads/*:refs/heads/*", {WILD}, "wildcard expands to many refs"),
        ("origin main feature-x", {NAME}, "ALL refspecs scanned, one protected suffices"),
        ("--repo=origin", {BARE}, "remote in the flag, nothing names a branch — bare"),
        ("--repo origin", {BARE}, "separated repo value consumed, still bare"),
        ("--repo=origin main", {NAME}, "remote in flag, so the sole positional is a refspec"),
        ("--all origin", {MIRROR}, "all-branches flag; single-arg deliberately suppressed"),
        ("--mirror", {MIRROR}, "mirror pushes everything"),
        ("--force origin feature-x", set(), "boolean flag — force to a feature branch is allowed"),
        ("-fq origin feature-x", set(), "short bundle of modelled no-value options"),
        (
            "--force-with-lease origin feature-x",
            set(),
            "optional-value option: git binds a value in attached form only",
        ),
        ("-o ci.skip origin feature-x", set(), "arity: -o consumes its separated value"),
        (
            "--push-option ci.skip origin main",
            {NAME},
            "value consumed, the real protected target still read",
        ),
        ("--push-option=ci.skip origin feature-x", set(), "attached value cannot leak"),
        (
            "--repo=origin --push-option ci.skip",
            {BARE},
            "the #7796 erasure row: value consumed, bare tag preserved",
        ),
        (
            "--receive-pack /usr/bin/git-receive-pack origin feature-x",
            set(),
            "arity: --receive-pack consumes its separated value",
        ),
        (
            "--exec /usr/bin/git-receive-pack origin main",
            {NAME},
            "arity: --exec consumes its value, protected target still read",
        ),
        ("-- origin main", {NAME}, "end-of-options: everything after -- is positional"),
        ("-- origin feature-x", set(), "end-of-options with a feature target stays allowed"),
        (
            "-- origin -x",
            set(),
            "a dash-named refspec after -- is positional, not an option",
        ),
        (
            "--frobnicate ci.skip origin feature-x",
            {BARE, SINGLE},
            "unrecognised option: split untrusted, BOTH no-refspec rows (the "
            "positionals may be values or a remote — rounds 10/13/14)",
        ),
        (
            "--frobnicate ci.skip origin main",
            {BARE, SINGLE, NAME},
            "protective reading scans every positional, precise tag preserved",
        ),
        ("--frobnicate", {BARE}, "unrecognised and nothing positional — bare either way"),
        (
            "--all --frobnicate v",
            {MIRROR},
            "fallback does not stack bare onto mirror-all (finding 3 suppression)",
        ),
        (
            "--repo=origin --push-option ma*",
            {BARE, WILD},
            "a glob-capable consumed value keeps the wildcard identity: pathname "
            "expansion can hand git words this scan never saw",
        ),
    )

    def test_the_parser_branch_table(self):
        for args, expected, why in self.TABLE:
            cmd = f"git push {args}".strip()
            got = security._git_publish_floor_tags(cmd)
            assert got == frozenset(
                expected
            ), f"{cmd!r}: expected {set(expected) or '{}'} because {why}; got {set(got) or '{}'}"

    def test_substitution_glue_stays_on_the_ungated_branch(self):
        # Kept out of TABLE because the sentinel is not a catalog row: the
        # anti-obfuscation branches are upstream of this parser and unaffected.
        got = security._git_publish_floor_tags("git push origin $(echo main)")
        assert got == frozenset({security._GIT_PUBLISH_UNGATED})


class TestTokenizerContractBetweenTheLayers:
    """The seam that broke: what the argument scan is handed, and by whom.

    Two changes to this gate landed independently and crossed. One taught the
    parse to resolve a git program hidden behind glued operators and to step over
    redirections; the other added the option-arity and shell-syntax model, which
    is defined over RAW words — it splits each word at its own unquoted operators
    and models redirection arity itself. Because neither textually conflicted, the
    outer parse silently pre-chewed the words the inner model reads, and six
    shapes lost the identity they are classified by. These pin the contract in
    both directions so the seam cannot drift apart again.
    """

    def test_the_argument_scan_is_handed_raw_words(self):
        # The shapes whose identity lives in the operator: cutting them here
        # turned each into a plain word and erased its tag.
        from kiro_crew.security import _git_push_args

        assert _git_push_args("git push origin>/dev/null") == ["origin>/dev/null"]
        assert _git_push_args("git push origin @(main)") == ["origin", "@(main)"]
        assert _git_push_args("git push origin & echo x") == ["origin", "&", "echo", "x"]
        assert _git_push_args("git push --all>/dev/null") == ["--all>/dev/null"]

    def test_redirection_arity_comes_from_one_model(self):
        # ``<<-`` is a complete operator, so its delimiter is a SEPARATED word.
        # A second reading of the grammar here read the ``-`` as an attached
        # target and left ``EOF`` behind as a phantom refspec.
        from kiro_crew.security import _git_push_args

        assert _git_push_args("git push origin <<- EOF") == ["origin"]
        # Still stepped over with the shell's arity, and the words behind a
        # redirection still survive as refspecs.
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
        assert _git_push_args("git push > >(git push origin my-feature)") == []

    def test_process_substitution_is_a_word_not_a_redirection(self):
        # ``<(`` / ``>(`` at the start of a word is process substitution, which
        # bash substitutes as a /dev/fd path WORD. Consuming it as a redirection
        # ate an option's value and shifted the positional split onto the remote.
        from kiro_crew.security import _git_push_args

        assert _git_push_args("git push -o <(echo) origin main") == [
            "-o",
            "<(echo)",
            "origin",
            "main",
        ]
        # In a word position it is unverifiable; as a redirection TARGET the shell
        # removes it, so an ordinary feature push whose output is teed stays
        # allowed. Both readings, from one place.
        assert security._GIT_PUBLISH_UNGATED in security._git_publish_floor_tags(
            "git push -o <(echo) origin main"
        )
        assert not security._git_publish_floor_tags("git push origin my-feature > >(tee log.txt)")
        assert security._git_publish_floor_tags("git push > >(git push origin my-feature)") == {
            "git-publish-push-bare"
        }

    def test_a_quoted_separator_does_not_split_the_segment(self):
        # Splitting on a quoted separator truncated the word mid-quote, and the
        # fragment then arrived with the shell state open — which the fragment
        # rule reads as a word fused across the boundary. Legal, unprotected
        # branch names were denied by the protective fallback.
        from kiro_crew.security import _split_push_command_segments

        assert _split_push_command_segments("git push origin 'feature|x'") == [
            "git push origin 'feature|x'"
        ]
        for cmd in (
            "git push origin 'feature|x'",
            "git push origin 'a;b'",
            "git push origin 'a&&b'",
            'git push origin "a||b"',
        ):
            assert not security._git_publish_floor_tags(cmd), cmd
        # An UNQUOTED separator still separates, and each side is judged alone.
        assert _split_push_command_segments("git push origin | cat") == [
            "git push origin ",
            " cat",
        ]
        assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
            "echo x | git push origin main"
        )
        # A quoted separator cannot smuggle a protected name past the scan: it
        # stays inside the word, so the refspec contains it and is not the name.
        assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
            "git push origin 'x' ; git push origin main"
        )

    def test_a_backslash_newline_still_splices_unreconstructably(self):
        # The one separator that VANISHES, so the words on either side fuse into
        # one this segment cannot reconstruct. It must keep splitting, and the
        # trailing escape must survive into the segment it ends — that escape is
        # the signal the ungated sentinel is drawn from.
        from kiro_crew.security import _split_push_command_segments

        assert _split_push_command_segments("git push origin ma\\\nin") == [
            "git push origin ma\\",
            "in",
        ]
        for cmd in ("git push origin ma\\\nin", 'git push origin "ma\\\nin"'):
            assert security._GIT_PUBLISH_UNGATED in security._git_publish_floor_tags(cmd), cmd

    def test_a_wrapped_payload_is_one_word_not_a_bare_git_token(self):
        # ``str.split`` tore a wrapper's quoted payload into fragments, so the
        # OUTER line — which is not itself a push — parsed as one and its
        # fragmented ref denied ordinary work. The payload is one word; the
        # caller judges it on its own, which is where the denial still comes from.
        from kiro_crew.security import _git_push_args, _split_shell_words, is_denied

        assert _split_shell_words("bash -c '(cd /tmp && git push origin my-feature)'") == [
            "bash",
            "-c",
            "'(cd /tmp && git push origin my-feature)'",
        ]
        assert _git_push_args("bash -c '(cd /tmp && git push origin my-feature)'") is None
        assert is_denied("bash -c '(cd /tmp && git push origin my-feature)'") is None
        assert is_denied("bash -c '(cd /tmp && git push origin main)'") is not None

    def test_stripping_punctuation_never_reclassifies_the_word(self):
        """A punctuation strip changes WHERE a word came from, not WHAT it is.

        The subshell-punctuation branch recovered the word in front of a ``)``
        and appended it to the positional list directly, skipping the
        option-vs-positional classifier every bare word goes through. So
        ``(git push --repo=origin -f)`` filed ``-f`` as the only refspec, it
        matched no protected name, and the segment came back with NO tags — a
        force push to a possibly-protected current branch, admitted by adding
        one parenthesis. ``(git push -f)`` reported the remote-only row instead
        of bare, the wrong-identity hazard three earlier rounds each turned into
        a bypass. Every glue branch that recovers a word now routes it through
        the one classifier, so the paired spellings agree by construction.
        """
        floor = security._git_publish_floor_tags
        for parenthesised, bare in (
            # The reported bypass, and its wrong-row sibling.
            ("(git push --repo=origin -f)", "git push --repo=origin -f"),
            ("(git push -f)", "git push -f"),
            ("(git push --repo=origin --force)", "git push --repo=origin --force"),
            ("(git push -qf)", "git push -qf"),
            # A value-taking option must still consume its separated value.
            ("(git push -o ci.skip origin main)", "git push -o ci.skip origin main"),
            (
                "(git push --push-option ci.skip origin main)",
                "git push --push-option ci.skip origin main",
            ),
            # End-of-options, all-branches, and an unmodelled option.
            ("(git push -- origin main)", "git push -- origin main"),
            ("(git push --all)", "git push --all"),
            ("(git push --frobnicate origin main)", "git push --frobnicate origin main"),
            # A GENUINE positional keeps being read as one, protected or not.
            ("(git push origin main)", "git push origin main"),
            ("(git push origin refs/heads/main)", "git push origin refs/heads/main"),
            ("(git push -f origin main)", "git push -f origin main"),
            ("(git push origin my-feature)", "git push origin my-feature"),
            ("(git push origin 'release/x')", "git push origin 'release/x'"),
        ):
            assert floor(parenthesised) == floor(bare), (
                f"{parenthesised!r}: the paren strip changed the reading — "
                f"{set(floor(parenthesised))} vs {set(floor(bare))}"
            )
        # Stated absolutely, not only as a pair: a force push with no refspec
        # names no branch, so the bare row must fire whatever the punctuation.
        for cmd in (
            "(git push --repo=origin -f)",
            "(git push -f)",
            "(git push --repo=origin -f --quiet)",
        ):
            assert "git-publish-push-bare" in security._git_publish_floor_tags(cmd), cmd

    def test_the_other_glue_branches_do_not_reclassify_either(self):
        # The sibling shapes that recover a word from glue: a glued redirection
        # keeps its own guard against a flag-shaped prefix, and the bare-operator
        # fallback marks the split untrusted before it scans the pieces, so a
        # flag landing among the candidates can only ADD a tag. Pinned so a
        # future edit cannot quietly turn one of them into an allow.
        for cmd in (
            "git push --repo=origin -f>/dev/null",
            "git push --repo=origin -f&",
            "git push --repo=origin -f <<- EOF",
            "git push --repo=origin -f 2>&1",
            "git push --repo=origin -f)",
            "git push -f)",
        ):
            assert "git-publish-push-bare" in security._git_publish_floor_tags(cmd), (
                f"{cmd!r}: a force push naming no refspec lost the bare row: "
                f"{set(security._git_publish_floor_tags(cmd))}"
            )


class TestProcessSubstitutionBoundaryIsProven:
    """The boundary rule: a PROVEN-complete substitution is a word, an
    UNPROVABLE one is ambiguous.

    Process substitution sits on a paren-depth walk, not the whole-segment expansion
    regex's fail-closed ``[<>]\\(``, so that a substitution
    the shell REMOVES (the target of ``> >(tee log)``) could keep the precise
    reading instead of denying an ordinary teed push. That walk counted parens
    with ``str.count``, which is quote-UNAWARE -- and a quoted paren inflates the
    depth, so the real closer never returns it to zero and every following word
    is swallowed into the substitution. Both review lanes traced it to a
    protected-branch push being allowed. The walk now shares the module's one
    quote/escape state machine and fails closed when the boundary cannot be
    proven, which restores the old regex's posture for exactly the unprovable
    cases while keeping the win for the provable ones.
    """

    def test_a_quoted_paren_cannot_swallow_the_refspec(self):
        # Both lanes' traces, verbatim. The quoted '(' contributes NOTHING to the
        # depth, so the substitution ends at its real ')' and 'main' survives as
        # the refspec it is.
        for cmd in (
            "git push origin feature > >(echo '(' ) main",
            'git push origin feature > >(printf "(") main',
            "git push origin feature > >(echo '((((' ) main",
            'git push origin feature > >(cat "((") main',
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert "git-publish-push-protected-branch-name" in tags, (
                f"{cmd!r}: a quoted paren inflated the substitution boundary and "
                f"swallowed the protected refspec: {set(tags)}"
            )
            assert tags, f"{cmd!r}: publishes main and was ALLOWED"
        # The words really are recovered, not merely tagged by a fallback.
        assert security._git_push_args("git push origin feature > >(echo '(' ) main") == [
            "origin",
            "feature",
            "main",
        ]

    def test_an_unprovable_boundary_fails_closed(self):
        # The words ran out with the substitution still open, so the boundary
        # cannot be proven and the rest of the segment cannot be read. Silently
        # swallowing it is what hid a refspec; this is the ambiguity sentinel,
        # which no catalog row can switch off -- the posture the whole-segment
        # regex gave process substitution before the walk existed.
        for cmd in (
            "git push origin feature > >(echo main",
            "git push origin feature > >(echo '('",
            "git push origin main > >(tee log",
            "git push origin feature > >(echo 'unterminated",
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert (
                security._GIT_PUBLISH_UNGATED in tags
            ), f"{cmd!r}: an unclosed process substitution did not fail closed: {set(tags)}"
            assert security._git_push_args(cmd) is None, cmd

    def test_the_teed_push_round_one_unblocked_stays_unblocked(self):
        # The round-1 win: a process substitution the shell REMOVES as a
        # redirection target never reaches git's argv, so the push keeps its
        # precise reading. A feature push stays allowed; a protected one is still
        # reported by its own row, and a bare one still fails closed.
        assert not security._git_publish_floor_tags(
            "git push origin my-feature > >(tee log.txt)"
        ), "the teed feature push regressed to a denial"
        assert security._git_publish_floor_tags("git push origin main > >(tee log)") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )
        assert security._git_publish_floor_tags("git push > >(git push origin my-feature)") == {
            "git-publish-push-bare"
        }
        assert security._git_push_args("git push --force origin 2> >(cmd)") == [
            "--force",
            "origin",
        ]
        # In a WORD position it is still unverifiable -- that half is unchanged.
        assert security._GIT_PUBLISH_UNGATED in security._git_publish_floor_tags(
            "git push -o <(echo) origin main"
        )

    def test_quoted_parens_outside_a_substitution_stay_data(self):
        # git allows ( and ) in a refname, and a quoted paren is an ordinary
        # character. The walk must not read one as shell structure in either
        # direction: these are legal unprotected names and stay pushable, while
        # a protected name inside a subshell is still reported.
        for cmd in (
            "git push origin '('",
            'git push origin ")"',
            "git push origin 'feat(x)'",
            "(git push origin ')')",
            "(cd /tmp; git push origin my-feature)",
        ):
            assert not security._git_publish_floor_tags(cmd), cmd
        assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
            "(git push origin main)"
        )
        # An ANSI-C quoted paren carries a '$', which the per-token pre-check
        # already ungates -- over-protective, and deliberately so.
        assert security._GIT_PUBLISH_UNGATED in security._git_publish_floor_tags(
            "git push origin $'('"
        )

    def test_the_state_machine_ignores_quoted_parens(self):
        # The property the boundary rests on, stated directly on the walk.
        walk = security._shell_quote_walk
        for text, delta in (
            ("(", 1),
            (")", -1),
            ("'('", 0),
            ('"("', 0),
            ("$'('", 0),
            ("\\(", 0),
            (">(echo", 1),
            ("log)", -1),
            ('"(")', -1),
            ("'()'", 0),
        ):
            assert walk(text).paren_delta == delta, f"{text!r}: delta {walk(text).paren_delta}"
        # Quote state resumes across a whitespace boundary, which is why a
        # quoted word spanning one cannot desync the depth.
        assert walk("'a").end_state == 1
        assert walk("b'", state=1).end_state == 0
        assert walk("(", state=1).paren_delta == 0

    def test_the_subshell_punctuation_branch_cannot_be_inflated(self):
        # The other branch also looks at parens, so it was audited for the same
        # shape. It is a character-MEMBERSHIP test over the operator tail, not a
        # depth count, and it is structurally immune: a quoted paren needs a
        # quote character in that tail, and a quote is not in "()", so the tail
        # stops qualifying as punctuation and the protective fallback takes it.
        # A quoted paren also produces no operator split at all, so such a token
        # never reaches the branch.
        assert security._push_token_shell_read("main')'") == (None, False)
        # With no split, it reads as the literal refname `main)` -- a different,
        # unprotected branch, so allowing it is the correct precise reading.
        assert not security._git_publish_floor_tags("git push origin main')'")
        # When a quoted paren DOES sit in the operator tail, the tail stops
        # qualifying as punctuation and the protective fallback takes the token.
        for cmd in (
            "git push origin main)'('",
            "git push origin main)'x'",
        ):
            assert security._git_publish_floor_tags(cmd), f"{cmd!r} was allowed"
        # And the branch's own case still reads precisely.
        assert security._git_publish_floor_tags("(git push origin main)") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )


class TestOneQuoteModelOnThePushPath:
    """Every push-path reading of shell quoting walks ONE state machine.

    This module has now been cured of the same defect three times: two
    tokenizers with different word models, two paren counters
    with different quote models, and finally two QUOTE models -- the word
    splitter had no ANSI-C (``$'...'``) awareness while the boundary walk did.
    In ``git push origin feature > >(echo $'a\\'b') main`` the splitter read the
    ESCAPED quote as a real closer, reopened on the next quote, and fused the
    trailing ``main`` into one unterminated word; the boundary walk then proved
    its parenthesis correctly but could not rescue a refspec that was
    already inside the word it had been handed. The cure is structural rather
    than another flag: :func:`_iter_shell_chars` owns the machine and every
    consumer drives it, so there is no second opinion left to drift from.
    """

    def test_an_ansi_c_escaped_quote_cannot_fuse_the_refspec(self):
        # The trace verbatim. A backslash inside $'...' ESCAPES, so the word ends
        # at its real closing quote and `main` stays the separate word it is.
        for cmd in (
            "git push origin feature > >(echo $'a\\'b') main",
            "git push origin feature > >(echo $'a\\'b') main extra",
            "git push origin feature > >(printf $'\\'') main",
            "git push origin feature > >(echo $'a\\'b\\'c') main",
        ):
            tags = security._git_publish_floor_tags(cmd)
            assert "git-publish-push-protected-branch-name" in tags, (
                f"{cmd!r}: an ANSI-C escaped quote desynced the word split and "
                f"fused the protected refspec: {set(tags)}"
            )
            assert tags, f"{cmd!r}: publishes main and was ALLOWED"
        assert security._git_push_args("git push origin feature > >(echo $'a\\'b') main") == [
            "origin",
            "feature",
            "main",
        ]

    def test_the_word_split_and_the_boundary_walk_agree_on_ansi_c(self):
        # The property that makes the class impossible: both readings come from
        # one machine, so they cannot disagree about where a quote ends.
        for token in (
            "$'a\\'b'",
            "$'\\''",
            "$'('",
            "$')'",
            "$'a\\'b'$'c\\'d'",  # adjacent ANSI-C words concatenate into one
            "$'a\\'b'\"x\"",  # ANSI-C followed by ordinary quoting
            "$'\\\\'",
        ):
            walk = security._shell_quote_walk(token)
            assert walk.end_state == 0, f"{token!r}: walk left the quote open"
            # The splitter must agree: a closed word does not swallow its neighbour.
            assert security._split_shell_words(f"x {token} y") == ["x", token, "y"], token
        # The CONTRAST that proves the ANSI-C branch is what differs: in a PLAIN
        # single quote a backslash is literal, so the quote really does close and
        # the next one really does reopen -- and both readings say so together.
        plain = "'a\\'b'"
        assert security._shell_quote_walk(plain).end_state == 1
        assert security._split_shell_words(f"x {plain} y") == ["x", f"{plain} y"]

    def test_ansi_c_parens_do_not_move_a_substitution_boundary(self):
        # An ANSI-C quoted paren is data, exactly like a plainly quoted one, so it
        # contributes nothing to the depth in either direction.
        for token in ("$'('", "$')'", "$'(('", "$'()'"):
            assert security._shell_quote_walk(token).paren_delta == 0, token
        for cmd in (
            "git push origin feature > >(echo $'(' ) main",
            "git push origin feature > >(echo $')' ) main",
            "git push origin feature > >(echo $'((((' ) main",
        ):
            assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
                cmd
            ), cmd
        # Outside a substitution the '$' pre-check already ungates it, which is
        # over-protective and deliberately so.
        assert security._GIT_PUBLISH_UNGATED in security._git_publish_floor_tags(
            "git push origin $'('"
        )

    def test_the_operator_cut_honours_escapes_through_the_same_machine(self):
        # _cut_at_operator was the fourth walker: it tracked quotes but not
        # escapes at all. Its pinned readings are unchanged, and an ESCAPED
        # operator now stays in the word -- bash hands git the ref `ma)in`.
        cut = security._cut_at_operator
        assert cut("(git") == "git"
        assert cut("main)&") == "main"
        assert cut("mainline)>log") == "mainline"
        assert cut("my-feature") == "my-feature"
        assert cut("'(main)'") == "'(main)'"
        assert cut('"(main)"') == '"(main)"'
        assert cut("ma\\)in") == "ma\\)in"
        assert security._dequote_token("ma\\)in") == "ma)in"

    def test_every_earlier_round_still_holds_under_the_shared_machine(self):
        # The unification must not cost any win the earlier rounds bought.
        floor = security._git_publish_floor_tags
        assert not floor("git push origin my-feature > >(tee log.txt)")
        assert floor("git push origin main > >(tee log)") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )
        assert floor("(git push --repo=origin -f)") == floor("git push --repo=origin -f")
        assert floor("(git push origin main)") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )
        assert not floor("git push origin 'feature|x'")
        assert not floor("git push origin 'a;b'")
        assert floor("git push origin <<- EOF") == frozenset({"git-publish-push-single-arg"})
        assert "git-publish-push-wildcard-refspec" in floor("git push origin @(main)")
        assert security._GIT_PUBLISH_UNGATED in floor("git push origin ma\\\nin")
        assert security._git_push_args("bash -c '(cd /tmp && git push origin my-feature)'") is None


class TestNestedPayloadExtractionSpansTheProvenBoundary:
    """The body extracted for nested scanning must span the boundary the walk PROVES.

    Rounds 3 and 4 gave the git-publish boundary walk and the word splitter one
    quote-aware machine. The nested-payload EXTRACTOR still walked parens on its
    own, and a quote-unaware span there does not merely mis-size the body -- it
    loses the nested command outright: ``git push origin my-feature > >(X=')' git
    push origin main)`` extracted ``X='``, so the publish of a protected branch
    inside the substitution was never scanned and ``is_denied`` returned None for
    a command bash executes. Extraction and boundary now read the same span, which
    is the only arrangement in which they cannot disagree.
    """

    def test_a_quoted_paren_cannot_hide_a_nested_push(self):
        # The trace verbatim, plus the quoting variants and nesting.
        for cmd in (
            "git push origin my-feature > >(X=')' git push origin main)",
            'git push origin my-feature > >(X=")" git push origin main)',
            "git push origin my-feature > >(X=$')' git push origin main)",
            "git push origin my-feature > >(X=')' Y=')' git push origin main)",
            "git push origin my-feature > >(X=')' git push origin main Y=')')",
            "git push origin my-feature > >(echo >(git push origin main))",
            "git push origin my-feature > >(echo >(X=')' git push origin main))",
            "cat <(X=')' git push origin main)",
        ):
            assert security.is_denied(cmd) is not None, (
                f"{cmd!r}: a quoted paren truncated the extracted body and the "
                f"nested protected push was never scanned"
            )
        # The control: the same shape WITHOUT the quoted paren was always denied,
        # so the quoting is what the fix restores rather than a new denial.
        assert (
            security.is_denied("git push origin my-feature > >(git push origin main)") is not None
        )

    def test_the_extracted_body_is_the_whole_body(self):
        from kiro_crew.security import _substitution_bodies

        assert _substitution_bodies(">(X=')' git push origin main)") == [
            "X=')' git push origin main"
        ]
        assert _substitution_bodies("$(X=')' echo hi)") == ["X=')' echo hi"]
        assert _substitution_bodies("<(a)<(b)") == ["a", "b"]
        # Nested substitutions: a PROVEN span yields ONE body per top-level
        # opener. The inner opener is reached when the payload walk recurses into
        # that body, so the extractor skips PAST a proven span instead of running
        # a fresh walk from every opener inside it (the per-opener rescan was
        # super-linear on the gate).
        assert _substitution_bodies(">(echo >(git push origin main))") == [
            "echo >(git push origin main)",
        ]
        assert "git push origin main" in security._shell_payload_sources(
            "cat >(echo >(git push origin main))"
        )
        # UNPROVEN span -> the whole remainder, the fail-closed direction, plus the
        # span up to the last ``)``.
        assert _substitution_bodies(">(git push origin main") == ["git push origin main"]
        assert _substitution_bodies(">(X=')' git push origin main") == [
            "X=')' git push origin main",
            "X='",
        ]

    def test_the_span_helper_is_quote_aware_and_reports_proof(self):
        from kiro_crew.security import _matching_close_paren

        # The text is the slice just past a ">(" / "$(" opener.
        assert _matching_close_paren("a)") == (2, True)
        assert _matching_close_paren("a(b))") == (5, True)
        # A quoted closer is data and must not end the span.
        assert _matching_close_paren("X=')' a)") == (8, True)
        assert _matching_close_paren('X=")" a)') == (8, True)
        assert _matching_close_paren("X=$')' a)") == (9, True)
        # A quoted OPENER must not deepen it either.
        assert _matching_close_paren("X='(' a)") == (8, True)
        # Never balanced -> not proven, and the span runs to the end.
        assert _matching_close_paren("a") == (1, False)
        assert _matching_close_paren("X=')' a") == (7, False)

    def test_the_self_protection_substitution_scan_is_quote_aware_too(self):
        # Same class at the self-protection rule: its private str.count walk
        # stopped at a quoted ')', so a name after it was never seen. That is an
        # UNDER-deny for this rule -- correcting an earlier audit note on this
        # line, which had recorded the truncation as over-deny-only.
        from kiro_crew.security import _protected_name_in_substitution

        # The quoted ')' must sit in a LATER token to reach the close test: a
        # private str.count walk drove depth to 0 there and BROKE, so the name
        # after it was never seen.
        tokens = ["$(true", "X=')'", "kirocrew", "token)"]
        assert _protected_name_in_substitution(tokens, 0) == "kirocrew"
        # Still stops at the REAL closer rather than reading a later command.
        assert _protected_name_in_substitution(["$(true)", ";", "kirocrew"], 0) == ""

    def test_every_earlier_round_still_holds(self):
        floor = security._git_publish_floor_tags
        assert not floor("git push origin my-feature > >(tee log.txt)")
        assert floor("git push origin main > >(tee log)") == frozenset(
            {"git-publish-push-protected-branch-name"}
        )
        assert security.is_denied("git push origin my-feature > >(tee log.txt)") is None
        assert floor("(git push --repo=origin -f)") == floor("git push --repo=origin -f")
        assert not floor("git push origin 'feature|x'")
        assert "git-publish-push-protected-branch-name" in floor(
            "git push origin feature > >(echo '(' ) main"
        )
        assert "git-publish-push-protected-branch-name" in floor(
            "git push origin feature > >(echo $'a\\'b') main"
        )
        assert security._GIT_PUBLISH_UNGATED in floor("git push origin feature > >(echo main")
        assert security._git_push_args("bash -c '(cd /tmp && git push origin my-feature)'") is None


class TestSubstitutionInsideDoubleQuotesIsExtractedFromAFreshContext:
    """A ``$(`` inside a double-quoted word starts a FRESH quoting context.

    bash parses a substitution body on its own, so ``"$(echo ")")"`` holds the
    body ``echo ")"``. The extractor walked the span from the start of the whole
    command instead, so the outer ``"`` made every ``)`` in the body read as
    quoted. Two failures followed from the one mis-delimit:

    * the span never proved, so the body swallowed the rest of the line and an
      ordinary ``git commit -m "$(date)" && git push origin feat`` was refused
      as an unverifiable push;
    * a ``"`` inside the body flipped the outer state, so a nested wrapper after
      it was cut out of the body: ``echo "$(echo ")"; bash -c '<push main>')"``
      was allowed although bash runs the push.
    """

    def test_the_body_is_delimited_at_its_own_closer(self):
        from kiro_crew.security import _substitution_bodies

        assert _substitution_bodies('git commit -m "$(date)" && x') == ["date"]
        assert _substitution_bodies('echo "$(echo ")")"') == ['echo ")"']
        # A heredoc body inside the span leaves it unproven: the whole
        # remainder, the span up to the last ")", and -- since the span still
        # has a real close -- its proven-close body NON-suppressed, so a nested
        # opener bash runs after the heredoc body is extracted.
        heredoc = "git commit -m \"$(cat <<'eof'\nsubject\neof\n)\" && x"
        assert _substitution_bodies(heredoc) == [
            "cat <<'eof'\nsubject\neof\n)\" && x",
            "cat <<'eof'\nsubject\neof\n",
            "cat <<'eof'\nsubject\neof\n",
        ]
        # An unbalanced body is still the whole remainder, the fail-closed side.
        assert _substitution_bodies('echo "$(date" && x') == ['date" && x']

    def test_a_feature_push_after_a_quoted_substitution_is_allowed(self):
        for cmd in (
            'git commit -m "$(date)" && git push origin feat',
            'echo "$(date)"; git push origin feat',
            'git commit -m "fix $(git rev-parse --short HEAD)" && git push -u origin feat',
        ):
            assert security.is_denied(cmd) is None, cmd

    def test_a_protected_push_around_a_quoted_substitution_stays_denied(self):
        for cmd in (
            'git commit -m "$(date)" && git push origin main',
            'git commit -m "$(date)" && git push',
            'git commit -m "$(date)" && git push origin $b',
            'git commit -m "$(date)" && git push origin {feat,main}',
            "git commit -m \"$(date)\" && git push origin $'main'",
            'git commit -m "$(git push origin main)"',
            'echo "$(echo ")"; bash -c \'git push origin main\')"',
            "echo \"$(echo ')'; eval 'git push origin main')\"",
            "echo '$(' \"$(bash -c 'git push origin main')\" ')'",
        ):
            assert security.is_denied(cmd) is not None, cmd

    def test_a_heredoc_body_inside_the_substitution_stays_scanned(self):
        msg = 'git commit -m "$(cat <<{tag}\n{body}\n{end}\n)" && git push origin feat'
        for tag, body, end in (
            ("'EOF'", "git push origin main", "EOF"),
            ("EOF", "git push origin main", "EOF"),
            ('"EOF"', "git push origin $b", "EOF"),
            ("'EOF'", "git push origin {x,main}", "EOF"),
            ("'EOF'", "git push origin main", "  EOF"),
            ("-'EOF'", "\tgit push origin main", "\tEOF"),
            ("EOF", "$(git push origin main)", "EOF"),
            ("'EOF'", ")\nEOF\nbash -c 'git push origin main'", "x"),
        ):
            cmd = msg.format(tag=tag, body=body, end=end)
            assert security.is_denied(cmd) is not None, cmd
        unterminated = (
            "git commit -m \"$(cat <<'EOF'\ngit push origin main\n)\" && git push origin feat"
        )
        assert security.is_denied(unterminated) is not None


class TestANestedConstructDoesNotHideAPushBehindItsOwnQuoting:
    """A substitution body, a ``${...}`` expansion and a backtick each open their
    OWN quoting context, so a ``)`` or ``"`` inside one is DATA, not the close of
    the enclosing ``$(``. The matcher skips each as a unit; without that, the
    inner quoting leaks out and closes the span early, so a ``bash -c '<push>'``
    sitting after the real close goes unscanned. A heredoc body is data too, but
    the matcher does not parse it: a body starting inside the span leaves the
    span unproven, so the whole remainder is scanned.

    The exact command that regressed:
    ``echo "$(echo "$(echo ")")" ; bash -c 'git push origin main')"`` -- bash runs
    the push (the first ``)`` after the inner ``"`` closed the span and the body
    came back as ``echo "$(echo "``). Every row below is a command bash really
    runs a protected push for, generated across nesting depth, the construct that
    carries the ``)``, the launcher and the push target.
    """

    _OPUS = 'echo "$(echo "$(echo ")")" ; bash -c \'git push origin main\')"'

    def test_the_opus_command_is_denied(self) -> None:
        assert security.is_denied(self._OPUS) is not None

    def test_a_push_hidden_behind_any_nested_construct_is_denied(self) -> None:
        # Each CARRIER puts a bare ``)`` (or re-quote) in a context the matcher
        # must skip whole; a wrong delimit lets the launcher behind it run free.
        carriers = (
            'echo ")"',  # a nested $( whose body re-quotes
            "echo ')'",
            'echo "${x:-")"}"',  # a ${...} default value
            'echo "`echo ")"`"',  # a backtick sub
        )
        launchers = ("bash -c", "sh -c", "eval")
        targets = ("main", "mainline", "$b", "{feat,main}", "$'main'")
        for depth in (1, 2, 3):
            for carrier in carriers:
                for sep in (" ; ", "; ", " && ", "\n"):
                    for launcher in launchers:
                        for target in targets:
                            push = f"git push origin {target}"
                            inner = f"\"$({carrier}{sep}{launcher} '{push}')\""
                            cmd = inner
                            for _ in range(depth - 1):
                                cmd = f'"$(echo {cmd})"'
                            cmd = "echo " + cmd
                            assert security.is_denied(cmd) is not None, cmd
        # a bare ``git push`` (current branch) is protected too.
        assert security.is_denied('echo "$(echo ")" ; bash -c \'git push\')"') is not None

    def test_a_heredoc_body_holding_the_close_paren_is_denied(self) -> None:
        for sep in (" ; ", "; ", " && ", "\n"):
            cmd = (
                "echo \"$(cat <<'E'\n)\nE\n"
                + sep.lstrip("\n")
                + "bash -c 'git push origin main')\""
            )
            assert security.is_denied(cmd) is not None, cmd
        # the substitution closing right after the heredoc, push after it.
        assert (
            security.is_denied("git commit -m \"$(cat <<'E'\n)\nE\n)\" && git push origin main")
            is not None
        )

    def test_a_feature_push_after_a_quoted_substitution_stays_allowed(self) -> None:
        for cmd in (
            'git commit -m "$(date)" && git push origin feat',
            'echo "$(date)"; git push -u origin feat',
            'git commit -m "fix $(git rev-parse --short HEAD)" && git push origin feat',
            'echo "$(echo ")")" && git push origin feat',
        ):
            assert security.is_denied(cmd) is None, cmd

    def test_a_heredoc_left_pending_by_a_nested_substitution_is_denied(self) -> None:
        # ``$(cat <<E)`` closes before the newline, so bash reads the body from
        # the lines after it, in the ENCLOSING span; the ``)`` on a body line is
        # data and the ``bash -c`` push behind the body runs.
        push = "git push origin main"
        for inner in ("$(cat <<E)", "$(cat <<'E')", "<(cat <<E)", "${x:-$(cat <<E)}"):
            body = f"{inner}\n)\nE\nbash -c '{push}'"
            for cmd in (
                f'echo "$(echo {body})"',
                f'eval "$(echo {body})"',
                f'echo "`echo {body}`"',
                f"echo $(echo {body})",
            ):
                assert security.is_denied(cmd) is not None, cmd
        assert (
            security.is_denied("echo \"$(echo $(cat <<E)\n)\nE\nbash -c 'git push')\"") is not None
        )

    def test_a_heredoc_tag_in_any_quoting_leaves_the_span_unproven(self) -> None:
        # bash ends the body at the tag AFTER quote removal: ``$'\\x45'`` is ``E``
        # and ``"E\\"F"`` is ``E"F``. The matcher reads no tag at all, so no
        # spelling can make it end the body early.
        for tag, end in (("$'E'", "E"), ("$'\\x45'", "E"), ('"E\\"F"', 'E"F'), ("\\E", "E")):
            for push in ("git push origin main", "git push"):
                cmd = f"echo \"$(cat <<{tag}\n)\n{end}\nbash -c '{push}')\""
                assert security.is_denied(cmd) is not None, cmd

    def test_a_body_line_that_differs_only_in_case_does_not_end_the_body(self) -> None:
        # The gate reads lowercased text, but bash ends ``<<E`` only at ``E``:
        # a body line ``e`` is data, so the ``)`` after it is data too. The
        # matcher matches no terminator, so case cannot fool it.
        for tag, decoy in (("E", "e"), ("EOF", "eof"), ("'EOF'", "Eof")):
            end = tag.strip("'")
            cmd = f"echo \"$(cat <<{tag}\n{decoy}\n)\n{end}\nbash -c 'git push origin main')\""
            assert security.is_denied(cmd) is not None, cmd


class TestALessThanPairThatIsNotAHeredocKeepsTheSpan:
    """A here-string ``<<<`` is not a heredoc operator. Reading its tail as a
    heredoc tag skips a "body" up to a later line spelling the tag and reports a
    proven span past the ``)`` where bash closes the substitution."""

    def test_a_here_string_is_not_a_heredoc(self) -> None:
        from kiro_crew.security.shell_normalizer import _matching_close_paren

        # bash closes at the first ")" (index 10)
        assert _matching_close_paren("cat <<<x\n) ; P\nx\n)") == (10, True)

    def test_a_heredoc_after_a_here_string_is_still_seen(self) -> None:
        from kiro_crew.security.shell_normalizer import _matching_close_paren

        # the body's ")" is data, so the span is not proven at it.
        assert _matching_close_paren("cat <<<x <<E\n)\nE\n)")[1] is False


class TestAnArithmeticShiftIsAnExpectedRefusal:
    """EXPECTED REFUSALS, by maintainer decision. Arithmetic is not modelled:
    ``$((``/``((`` are read as plain parens, so a ``<<`` shift inside them counts
    as a heredoc like any other ``<<``. A shift followed by a newline before the
    span closes therefore leaves the span unproven, and its whole remainder is
    scanned in the outer quoting context -- an over-read, the fail-closed
    direction. The commands below run no push in bash (the ``bash -c`` text is a
    quoted ``echo`` argument), and they are refused on purpose rather than kept
    allowed; ``main`` refuses them too."""

    _PUSH = "git push origin main"

    def test_a_multi_line_shift_leaves_the_span_unproven(self) -> None:
        from kiro_crew.security.shell_normalizer import _matching_close_paren

        assert _matching_close_paren(" (( 1<<2 ))\n) ; P\n2\n)")[1] is False
        # ``$(( 1<<2\n))``: the body handed over starts with the second "(".
        assert _matching_close_paren("( 1<<2\n))\n2\n)")[1] is False
        assert _matching_close_paren("echo $(( 1<<2\n))\n) ; P\n2\n)")[1] is False
        # A heredoc after the arithmetic is unproven the same way.
        assert _matching_close_paren(" (( 1<<2 )); cat <<E\n)\nE\n)")[1] is False

    def test_push_text_after_a_multi_line_shift_is_refused(self) -> None:
        launch = f"\"bash -c '{self._PUSH}'\""
        for cmd in (
            f'echo "$( (( 1<<2 ))\necho hi)" {launch}',
            f'echo "$(( 1<<2\n))" {launch}',
            f'echo "$(echo $(( 1<<2\n)))" {launch}',
        ):
            assert security.is_denied(cmd) is not None, cmd

    def test_a_shift_closed_on_its_own_line_still_proves_the_span(self) -> None:
        from kiro_crew.security.shell_normalizer import _matching_close_paren

        # No body line is reached before the close, so the span is proven.
        assert _matching_close_paren(": ; ((1<<2)) ; date)\nx") == (
            len(": ; ((1<<2)) ; date)"),
            True,
        )
        assert security.is_denied('echo "$(: ; ((1<<2)) ; date)" && git push origin feat') is None
        assert security.is_denied("echo $(( 1<<2 )) && git push origin feat") is None


class TestAMisreadSpanFailsClosed:
    """Spellings the span matcher misread, each letting a command through that
    the gate refuses on main: a ``$[...]`` arithmetic shift read as a heredoc, a
    heredoc still open at a top-level ``)`` that left the rm floor's span
    unproven, a deep ``${a:-${a:-...}}`` chain that ran the walk out of recursion
    before the payload behind it was queued, a single-quoted ``$(`` whose span
    swallowed the real opener after it, and a bare ``{`` counted as nesting. So
    did a quoted ``<(`` or a ``$$`` read as an opener, a ``'`` carried out of an
    earlier span, and a capped span given no reading past its heredoc. And so
    did a ``'`` in a comment or a heredoc body, read as opening a quote; a capped
    span's remainder whose re-extraction was suppressed by its value; and a
    single-quoted backtick opened as a substitution. And a capped span a data
    ``)`` at the cap kept from ever closing, hiding a later top-level opener's
    push inside its value-suppressed remainder. And a capped span nested inside
    an outer body whose OWN text held the launcher, and a proven body that equals
    a suffix of its source, suppressed by that value."""

    _PUSH = "git push origin main"

    def test_an_arithmetic_bracket_shift_cannot_hide_a_push(self) -> None:
        cmd = f'echo "$(echo $[1<<x]\necho \'\nx]\n)\' ; bash -c "{self._PUSH}")"'
        assert security.is_denied(cmd) is not None

    def test_a_heredoc_open_at_the_close_keeps_the_rm_target(self) -> None:
        for cmd in (
            'rm -rf "$(cat <<E)" ~',
            "rm -rf \"$(cat <<'E')\" ~",
            'rm -rf "$(cat <<E\nx\nE\n)" ~',
        ):
            assert security.is_denied(cmd) is not None, cmd

    def test_a_deep_brace_chain_cannot_hide_a_push(self) -> None:
        from kiro_crew.security.shell_normalizer import _matching_close_paren

        deep = "${a:-" * 1000 + "x" + "}" * 1000
        assert security.is_denied(f"bash -c '{self._PUSH}'; echo $(echo {deep})") is not None
        assert security.is_denied(f'rm -rf "$(echo {deep})" ~') is not None
        # past the nesting cap the reading is unproven, never an exception
        assert _matching_close_paren(f"echo {deep})")[1] is False

    def test_a_single_quoted_opener_cannot_swallow_the_real_one(self) -> None:
        cmd = f"echo '$(' \"$(echo ')' ; bash -c '{self._PUSH}')\""
        assert security.is_denied(cmd) is not None

    def test_a_quote_in_a_comment_or_heredoc_cannot_hide_an_opener(self) -> None:
        payload = f"echo \"$(bash -c '{self._PUSH}')\" # '"
        for cmd in (
            f"# don't\n{payload}",
            f"echo hi # don't\n{payload}",
            f"cat <<E\ndon't\nE\n{payload}",
            f"cat <<<x # it's\n{payload}",
        ):
            assert security.is_denied(cmd) is not None, cmd

    def test_a_bare_double_paren_without_an_adjacent_close_is_two_subshells(self) -> None:
        from kiro_crew.security.shell_normalizer_span import _matching_close_paren

        # Arithmetic is not modelled, so the ``<<`` in ``((cat <<E`` is a
        # heredoc and its body's ``)`` must not prove the span.
        push = f"bash -c '{self._PUSH}'"
        for cmd in (
            f'echo "$(: ; ((cat <<E\n)\nE\n) ) ; {push})"',
            f'echo "$(((cat <<E\n)\nE\n) ) ; {push})"',
            f'echo "$(: ; ((cat <<E\n)\nE\n)) ; {push})"',
            f'x="$(: ; ((cat <<E\n)\nE\n) ) ; {push})"',
        ):
            assert security.is_denied(cmd) is not None, cmd
        # A heredoc whose tag line closes nothing keeps the span unproven.
        assert _matching_close_paren(": ; ((cat <<E\n)\nE\n) ) ; x)")[1] is False

    def test_a_double_quoted_process_sub_opener_is_data(self) -> None:
        # bash prints ``"<(echo '"`` as text and runs the push after it.
        push = f"bash -c '{self._PUSH}'"
        for cmd in (
            f'echo "<(echo \'" "$({push})" "\')"',
            f'echo ">(echo \'" "$({push})" "\')"',
        ):
            assert security.is_denied(cmd) is not None, cmd
        assert security.is_denied('echo "<(x)" && git push origin feat') is None

    def test_a_run_command_after_a_heredoc_body_in_a_bare_span_is_denied(self) -> None:
        # A ``$(...)`` left unproven by a HEREDOC still has a real close, and a
        # ``<(bash -c '<push>')`` (or ``$(...)`` / backtick) bash RUNS can sit
        # after the body inside it: ``echo $(cat <<E\nE\ncat <(bash -c
        # '<push>'))``. The suppressed over-reads scanned that text but never
        # emitted the nested opener, so its push was no payload source and the
        # floor never ran. The span's proven-close body is emitted non-suppressed
        # now, so the opener is reached. ``main`` refuses these; the parent
        # commit allowed them.
        for inner in ("<(bash -c '{p}')", "$(bash -c '{p}')", "`bash -c '{p}'`"):
            launch = inner.format(p=self._PUSH)
            for cmd in (
                f"echo $(cat <<E\nE\ncat {launch})",
                f'echo "$(cat <<E\nx\nE\ncat {launch})"',
            ):
                assert security.is_denied(cmd) is not None, cmd
        # A self-restart reached the same way trips the restart floor.
        for cmd in (
            "echo $(cat <<E\nE\ncat <(kirocrew restart))",
            'echo "$(cat <<E\nx\nE\ncat <(kirocrew restart))"',
        ):
            assert security.is_denied(cmd) is not None, cmd
        # Harmless twins stay at main's verdict: a FEATURE push and an nginx
        # reload reached through the same heredoc span are allowed.
        assert (
            security.is_denied("echo $(cat <<E\nE\ncat <(bash -c 'git push origin feat/x'))")
            is None
        )
        assert security.is_denied("echo $(cat <<E\nE\ncat <(nginx -s reload))") is None

    def test_a_run_command_in_a_span_that_never_closes_is_denied(self) -> None:
        # An arithmetic ``<<`` before a newline is read as a heredoc with no
        # terminator line, so the outer ``$(...)`` runs to the END of the text:
        # there is no close to bound a body by, and every reading of it was a
        # suppressed over-read. The ``<(bash -c '<push>')`` bash runs after the
        # newline was then no payload source. The remainder is scanned once,
        # flat, now. ``main`` refuses these; the parent commit allowed them.
        for tail in (" ; true", ""):
            for inner in ("<(bash -c '{p}')", "$(bash -c '{p}')", "`bash -c '{p}'`"):
                launch = inner.format(p=self._PUSH)
                cmd = f"echo $(echo $((1<<2))\ncat {launch}){tail}"
                assert security.is_denied(cmd) is not None, cmd
        assert (
            security.is_denied("echo $(echo $((1<<2))\ncat <(kirocrew restart)) ; true") is not None
        )
        # Harmless twins stay at main's verdict.
        assert (
            security.is_denied(
                "echo $(echo $((1<<2))\ncat <(bash -c 'git push origin feat/x')) ; true"
            )
            is None
        )
        assert security.is_denied("echo $(echo $((1<<2))\ncat <(nginx -s reload)) ; true") is None

    def test_a_heredoc_quote_after_a_span_that_never_closes_hides_no_opener(self) -> None:
        # The never-close remainder can hold a REAL heredoc whose body line has
        # an unbalanced quote. Read as live, that quote swallowed the later
        # ``<(...)``/``>(...)``, so a self restart or self kill bash runs there
        # was no payload source. ``main`` refuses these; the parent allowed them.
        restart = "kirocrew " + "restart"
        for body in ("it's", "$'x\\'y"):
            for opener in ("cat <({c})", "echo x > >({c})"):
                for outer in ("echo $({x}) ; true", "x=$({x}) ; true", 'echo "$({x})" ; true'):
                    for c, denied in ((restart, True), ("nginx -s reload", False)):
                        inner = f"echo $((1<<2))\ncat <<E\n{body}\nE\n{opener.format(c=c)}"
                        cmd = outer.format(x=inner)
                        assert (security.is_denied(cmd) is not None) is denied, cmd

    def test_a_later_rm_after_a_span_that_never_closes_is_denied(self) -> None:
        # A double-quoted ``$(`` holding an arithmetic ``<<`` before a newline is
        # read as a heredoc with no terminator, so the span runs to the END of
        # the text. The rm floor then resumed at the end and never produced the
        # ``; rm -r -f $HOME`` bash runs after the real ``)``. Such a span now
        # refuses the whole command. ``main`` refuses the home and root rows;
        # the parent commit allowed them.
        span = '"$(echo $((1<<2))\n)"'
        for lead in ("echo ", "x=", "rm -f "):
            for sep in (" ; ", " && ", " | ", "\n"):
                for tail in ("rm -r -f $HOME", "rm -r -f ~", "rm -rf ${HOME}", "rm -r -f /"):
                    cmd = f"{lead}{span}{sep}{tail}"
                    assert security.is_denied(cmd) is not None, cmd
        # A span the reader proves keeps main's verdict on the later command.
        assert security.is_denied('echo "$(echo $((1<<2)))" ; rm -r -f /tmp/x') is None

    def test_a_self_kill_after_a_span_that_never_closes_is_denied(self) -> None:
        # The raw self-kill walk ended at a span the heredoc rule never closes
        # (``$((1<<2))`` + newline), so the later decoyed ``$k $(...)`` sibling
        # was never walked. It now resumes at the heredoc-agnostic close.
        # ``main`` refuses these; the parent commit allowed them.
        decoy = "$k $(printf ')' ; pgrep -f kirocrew)"
        for span in ("x=$(echo $((1<<2))\n); ", "x=$(cat <(echo $((1<<2))\n)); "):
            assert security.is_denied(f"k=kill; {span}{decoy}") is not None
            assert security.is_denied(f"k=kill; {span}{decoy.replace('kirocrew', 'nginx')}") is None

    def test_a_heredoc_terminator_glued_to_the_close_ends_the_body(self) -> None:
        # bash ends a heredoc inside ``$(...)`` at ``EOF)`` (bash-measured, with a
        # warning). Read as a body line instead, the span ran to the end and the
        # rm floor refused an ordinary commit message holding ``confirm``.
        for q in ("'EOF'", "EOF", '"EOF"'):
            msg = f'git commit -m "$(cat <<{q}\nfix: confirm the terminal format\nEOF)"'
            assert security.is_denied(msg) is None, msg
            assert security.is_denied(msg + " ; rm -rf ~") is not None
            assert security.is_denied(f'rm -rf "$(cat <<{q}\nx\nEOF)" ~') is not None
            assert security.is_denied(f'rm -rf "$(cat <<{q}\nx\nEOF)" /tmp/x') is None
        tab = 'git commit -m "$(cat <<-EOF\n\tfix: confirm\n\tEOF)"'
        assert security.is_denied(tab) is None

    def test_a_double_quoted_delimiter_keeps_a_backslash_bash_keeps(self) -> None:
        # Inside ``"..."`` (and ``$"..."``) bash drops a backslash only before
        # ``$``, a backtick, ``"``, ``\`` or a newline, so ``"E\q"`` is the
        # delimiter ``E\q`` (bash-measured). Dropping every backslash read it as
        # ``Eq``, closed the span at the body line ``Eq)"`` and hid the home
        # operand. ``main`` refuses the home rows; the parent commit allowed them.
        for quote in ('"', '$"'):
            for ch in ("q", "z"):
                delim = f'{quote}E\\{ch}"'
                body = f'"\nE{ch})"\nE\\{ch}\n)'
                for home in ("~", "$HOME", "/"):
                    cmd = f'rm -rf "$(cat <<{delim}\n{body}" {home}'
                    assert security.is_denied(cmd) is not None, cmd
                assert security.is_denied(f'rm -rf "$(cat <<{delim}\n{body}" /tmp/x') is None
        # An escapable character still loses its backslash: ``"E\$"`` ends at ``E$``.
        assert security.is_denied('rm -rf "$(cat <<"E\\$"\nx\nE$\n)" /tmp/x') is None
        assert security.is_denied('rm -rf "$(cat <<"E\\$"\nx\nE$\n)" ~') is not None

    def test_an_apostrophe_in_a_quoted_assignment_prefix_keeps_the_home_rm(self) -> None:
        # ``_in_assignment_value`` opened a single quote on a ``'`` even inside a
        # double-quoted word, so ``MSG="it's"`` desynced it: it read the unproven
        # span as an assignment VALUE and dropped the fail-closed home operand, so
        # ``rm -rf $HOME`` slipped. Double quotes are tracked now, so the ``rm``
        # is seen as a command and its home wipe refused. ``main`` refuses the
        # home rows; the parent commit allowed them.
        for span in ('"$(echo $(( 1<<2\n)))"', '"$(cat <<E\nx\n)"'):
            for home in ("~", "$HOME"):
                assert security.is_denied(f'MSG="it\'s" rm -rf {span} {home}') is not None
        # No-apostrophe control: refused on both.
        assert security.is_denied('MSG="its" rm -rf "$(echo $(( 1<<2\n)))" ~') is not None

    def test_an_ansi_c_heredoc_delimiter_is_decoded(self) -> None:
        # ``<<$'E'`` has delimiter ``E`` after ANSI-C quote removal, so the body
        # terminates at ``E`` and the span CLOSES. Reading it as the literal
        # ``$E`` left the span unproven to end of text, over-refusing a harmless
        # ``/tmp`` cleanup. The /tmp twin is allowed now (main's verdict); a home
        # operand after the same, now-proven span is still refused on both.
        tmp = "rm -rf \"$(cat <<$'E'\n/tmp/scratch-one\nE\n)\" /tmp/scratch-two"
        assert security.is_denied(tmp) is None
        assert security.is_denied("rm -rf \"$(cat <<$'E'\n/tmp/scratch-one\nE\n)\" ~") is not None
        # The locale ``$"E"`` spelling removes its quote the same way.
        assert security.is_denied('rm -rf "$(cat <<$"E"\n/tmp/one\nE\n)" /tmp/two') is None

    def test_an_arithmetic_shift_closing_late_in_a_quote_keeps_the_home_rm(self) -> None:
        # ``rm -r -f "$(echo $((1<<X))⏎)" ~ ; : '⏎X⏎)'`` -- the ``<<X`` in ``$((``
        # was recorded as a heredoc pending ``X``, so the body lines up to the
        # later ``X`` line were skipped as data and the span closed UNPROVEN at
        # the ``)`` inside the trailing single quote (``< len(body)``, so no
        # run-to-end ``_UnreadSpan``). The ``~`` after the real ``)`` was read as
        # heredoc data and never segmented, so bash's ``rm -r -f 1 ~`` slipped.
        # The span is unproven and its first ``<<`` follows an arithmetic opener,
        # so bash reads a shift there and the walk's close may be past the real
        # one: the floor refuses the whole command, however far the real ``)``
        # is. ``main`` refuses every home/root row; the parent commit allowed
        # them. A genuine heredoc span keeps its own reading.
        nl = "\n"

        def span(expr: str, pad: str = "") -> str:
            return '"$(echo $((' + expr + ")) " + pad + nl + ')"'

        for expr, pad in (("1<<X", ""), ("1 << X", ""), ("y = 1<<X", ""), ("1<<X", "a" * 400)):
            s = span(expr, pad)
            trailer = " ; : '" + nl + "X" + nl + ")'"
            # the operand glued DIRECTLY after the span (Opus's shape)
            for operand in ("~", "$HOME", "/"):
                cmd = "rm -r -f " + s + " " + operand + trailer
                assert security.is_denied(cmd) is not None, cmd
            # a second rm after a separator the real ``)`` was supposed to hide
            for sep in (" && ", " | ", "\n"):
                cmd = "rm -r -f " + s + sep + "rm -r -f ~" + trailer
                assert security.is_denied(cmd) is not None, cmd
            # assignment-value form before the real rm
            assert security.is_denied("x=" + s + "; rm -r -f ~" + trailer) is not None
        # a real heredoc FIRST does not hide a later arithmetic ``<<``
        decoy = '"$(cat <<E' + nl + "E" + nl + "echo $((1<<X))" + nl + ')"'
        for operand in ("~", "$HOME", "/"):
            cmd = "rm -r -f " + decoy + " " + operand + " ; : '" + nl + "X" + nl + ")'"
            assert security.is_denied(cmd) is not None, cmd
        # a genuine heredoc keeps its reading: home refused, /tmp allowed
        assert security.is_denied('rm -r -f "$(cat <<X\nd\nX\n)" ~') is not None
        assert security.is_denied('rm -r -f "$(cat <<X\nd\nX\n)" /tmp/x') is None

    def test_an_rm_target_after_a_capped_span_fails_closed(self) -> None:
        # Past the nesting cap the span's close is a guess, so the ``~`` after
        # it must not be lost.
        body = "x"
        for _ in range(18):
            body = '"$(echo ${x:-' + body + '})"'
        assert security.is_denied('rm -rf "$(echo ' + body + ')" ~') is not None
        assert security.is_denied("rm -rf \"$(cat <<'E'\nbuild\nE\n)\"") is None

    def test_a_capped_span_remainder_is_re_extracted_for_a_nested_push(self) -> None:
        # A span nested past the cap is read unproven and its whole remainder is
        # the only body emitted for it, so the payload walk MUST re-extract that
        # remainder: the real ``"$(...)"`` openers after the capped span live
        # only inside it. Suppressing re-extraction by the body VALUE (every
        # full-remainder suffix) let this push through.
        capped = "echo " + "$(echo " * 33 + "x" + ")" * 33
        hidden = 'echo "$(echo "$(bash -c \'git p""ush origin main\')")"'
        for cmd in (
            f"{capped} ; {hidden}",
            f'{capped} ; echo "$(bash -c \'git p""ush origin main\')"',
        ):
            assert security.is_denied(cmd) is not None, cmd
        # The same shape pushing a feature branch is still allowed.
        assert security.is_denied(f"{capped} ; git push origin feat") is None

    def test_a_capped_span_as_an_assignment_value_cannot_mask_a_home_rm(self) -> None:
        """A capped ``$(...)`` read past the nesting cap has its output treated as
        a home operand, so ``rm -rf "$(<capped>)" ~`` fails closed. But that
        synthetic ``~`` was inserted even when the span was a ``VAR="..."``
        assignment VALUE, where the span feeds a variable, not an ``rm``. In
        ``x="$(<capped>)" rm -rf $HOME`` the inserted ``~`` landed between the
        assignment and the real ``rm``, so the rm-floor read ``~`` as the command
        and ``rm -rf $HOME`` as its arguments and missed the home wipe. The
        synthetic operand is now added only in operand/command position, so the
        real ``rm`` is seen. ``main`` refuses every row; this head allowed them."""
        deep = "$(echo " * 33 + "x" + ")" * 33
        braces = '"$(echo ' * 18 + "${x:-x}" + ')"' * 18
        for span in (deep, braces):
            for assign in ('x="$(echo {s})"', 'a=1 b=2 x="$(echo {s})"'):
                prefix = assign.format(s=span)
                for home in ("~", "$HOME", '"$HOME"', "${HOME}", "~/", "~/*"):
                    for rm in ("rm -rf", "rm -r -f", "rm --recursive --force"):
                        cmd = f"{prefix} {rm} {home}"
                        assert security.is_denied(cmd) is not None, cmd
        # The capped span as an rm OPERAND still fails closed (the ``~`` it may
        # lose to the guessed close is restored synthetically).
        assert security.is_denied(f'rm -rf "{deep}" ~') is not None
        # A capped assignment value with no rm after it is still allowed, as on
        # main -- the fix narrows the synthetic operand, it does not add refusals.
        assert security.is_denied(f'x="$(echo {deep})"') is None
        assert security.is_denied(f'x="$(echo {deep})" echo hi') is None
        assert security.is_denied("rm -rf \"$(cat <<'E'\nbuild\nE\n)\"") is None
        # The assignment value was recognised by a START-ANCHORED regex, so any
        # text before the ``NAME="`` made it miss: the synthetic ``~`` was then
        # inserted into a real assignment value anyway, landing before the real
        # ``rm`` and masking the home wipe. Decided from word structure now, so a
        # separator-cut leading space, an earlier completed assignment, a partial
        # quoted value and ``+=`` are all read as the assignment value they are.
        # Every row below was ALLOWED on the parent commit and bash-wipes ``$HOME``.
        for prefix in (
            'true; x="$(echo {s})"',  # ``;``-cut leaves a leading space
            'true && x="$(echo {s})"',
            'true | x="$(echo {s})"',
            ' x="$(echo {s})"',  # a bare leading space
            'a="p q" x="$(echo {s})"',  # an earlier assignment with a quoted space
            '2>/dev/null x="$(echo {s})"',  # a leading redirection runs first
            '2> /dev/null x="$(echo {s})"',  # ... with a separate target word
            '</dev/null a=1 x="$(echo {s})"',
            'x="a$(echo {s})"',  # a partial value before the span
        ):
            for span in (deep, braces):
                cmd = f"{prefix.format(s=span)} rm -rf $HOME"
                assert security.is_denied(cmd) is not None, cmd
        # ``+=`` is an assignment value too, so no synthetic ``~`` is inserted
        # before the real ``rm`` -- it stays at main's verdict (main's env-strip
        # does not strip ``x+=``, so main allows it as well; bolichen97's
        # non-blocking ask). The point is parity with main, not a new refusal.
        assert security.is_denied(f'x+="$(echo {deep})" rm -rf $HOME') is None
        # A genuine command word before the span is NOT an assignment value, so
        # the operand failsafe still fires (parity with main).
        assert security.is_denied(f'echo "$(echo {deep})" rm -rf ~') is not None

    def test_a_heredoc_in_a_quoted_span_cannot_mask_a_home_rm(self) -> None:
        """A ``$(...)`` whose heredoc body holds an UNTERMINATED backtick left the
        span unproven AND made the quote-aware backtick scan swallow the rest of
        the command -- so the walk never reached the real ``)`` and the enclosing
        ``rm`` segment never saw the home operand after it. The backtick unit-skip
        now steps over just the one broken backtick and keeps scanning, so the
        span resumes at the real closer and the ``~``/``$HOME`` after it reaches
        the rm floor. ``main`` refuses every home row; the parent commit allowed
        them. The ``/tmp`` twin stays allowed on both."""
        # `rm -rf "$(cat <<E` + newline + "`" + newline + `E` + newline + `)" OP`
        for home in ("~", "$HOME", '"$HOME"', "${HOME}", "~/"):
            cmd = f'rm -rf "$(cat <<E\n`\nE\n)" {home}'
            assert security.is_denied(cmd) is not None, cmd
        # The same heredoc-masked span before a /tmp operand stays allowed, as on
        # main -- the fix reads the real operand, it does not blanket-refuse.
        assert security.is_denied('rm -rf "$(cat <<E\n`\nE\n)" /tmp/x') is None

    def test_a_heredoc_body_opener_cannot_mask_a_home_rm(self) -> None:
        """A ``${``/``${x``/``${x:-`` (or ``$(``/``<(``/``>(``) on a heredoc BODY
        line is stdin DATA the here-document prints, not a live nested unit. The
        span walk still unit-skipped it there, consuming the enclosing span's
        real ``)`` (the ``${`` with no ``}`` ran to the end), so ``rm_floor``
        never saw the operand after the span and a ``$HOME`` wipe was allowed.
        On a body line the walk now steps over just the opener char and leaves
        the span unproven, so it reaches the real close and the home operand
        after it reaches the rm floor. ``main`` refuses every home/root row; the
        parent commit allowed them. The ``/tmp`` twin stays allowed on both."""
        for opener in ('"$(cat <<E', '"$(cat <<-E', "\"$(cat <<'E'"):
            for body in ("${", "${x", "${x:-"):
                for home in ("~", "$HOME", '"$HOME"', "${HOME}", "~/", "/"):
                    cmd = f'rm -rf {opener}\n{body}\nE\n)" {home}'
                    assert security.is_denied(cmd) is not None, cmd
                # The /tmp twin of the same masked span stays allowed, as on
                # main -- the fix reads the real operand, not a blanket refusal.
                assert security.is_denied(f'rm -rf {opener}\n{body}\nE\n)" /tmp/x') is None, (
                    opener,
                    body,
                )

    def test_the_whole_heredoc_body_opener_family_cannot_mask_a_home_rm(self) -> None:
        """The 4th round closes the WHOLE body-char family, not one more char.

        A backtick (paired with a LATER backtick), a ``$(`` / ``<(`` / ``>(``
        opener and a bare ``(`` on a heredoc BODY line are all stdin DATA the
        here-document prints, not live units. Two readings had still leaked:

        * a backtick on a body line was paired with a later backtick (``; echo
          '`'``) through ``_backtick_closer``, skipping PAST the span's real
          ``)``;
        * a ``$(`` / ``<(`` / ``>(`` body opener stepped over only ONE char, so
          its ``(`` counted toward depth, and -- NESTED one level inside a quoted
          ``$(echo ...)`` -- that extra depth ate the ENCLOSING span's real ``)``
          (a bare ``(`` body line did the same).

        The walk now reads a body-line backtick as data at every nest level, and
        a body-line two-char opener / bare ``(`` as data one level or more deep,
        so the span reaches its real close and the operand after it reaches the
        rm floor. ``main`` refuses every home/root row below; the parent commit
        (``aca7ad2404``) allowed them. Every ``/tmp`` twin stays allowed on both,
        so the fix reads the real operand rather than blanket-refusing."""
        homes = ("~", "$HOME", '"$HOME"', "${HOME}", "~/", "/")
        for tag in ("E", "'E'"):
            # backtick with a LATER backtick clause (direct), and every opener /
            # bare ``(`` NESTED one level inside a quoted substitution.
            direct_bt = [(f'"$(cat <<{tag}\n`\nE\n)"', "; echo '`'")]
            nested = [
                (f'"$(echo "$(cat <<{tag}\n{body}\nE\n)")"', tail)
                for body in ("`", "(", "$(", "<(", ">(")
                for tail in ("", "; echo '`'")
            ]
            for span, tail in direct_bt + nested:
                for home in homes:
                    cmd = f"rm -rf {span} {home}{tail}"
                    assert security.is_denied(cmd) is not None, cmd
                # /tmp twin stays at main's verdict (allowed).
                assert security.is_denied(f"rm -rf {span} /tmp/x{tail}") is None, (span, tail)

    def test_the_heredoc_body_quote_family_cannot_mask_a_home_rm(self) -> None:
        """The quote characters the opener rounds did not list. A ``'`` / ``"`` /
        ANSI-C ``$'`` on a heredoc BODY line is printed stdin DATA, not a live
        quote -- but the span walk let ``_iter_shell_chars`` OPEN a quote there,
        and that quote state ran PAST the span's real ``)`` (a body ``it's``
        opened a single quote that swallowed the ``)" ~`` behind the span), so
        the home operand never reached the rm floor and a real wipe was allowed.

        The walk now reads a body-line quote as data, so the span reaches its
        real close and the operand after it reaches the floor. Below, every
        home/root row -- DIRECT, NESTED and POST-terminator (``x=...; rm -rf
        ~``), across ``<<E`` / ``<<'E'`` / ``<<"E"`` -- refuses in a real wipe
        the parent commit allowed, while every ``/tmp`` twin of a direct or
        nested span stays ALLOWED, so the fix reads the real operand rather than
        blanket-refusing. (A span the walk cannot close at all is still caught
        by the rm floor's unproven-to-end fail-closed net.)"""
        bodies = ("it's", 'say "hi', "$'x", "'", '"', "don't \"x")
        homes = ("~", "$HOME", '"$HOME"', "${HOME}", "/")
        for tag in ("E", "'E'", '"E"'):
            for body in bodies:
                span = f'"$(cat <<{tag}\n{body}\nE\n)"'
                nested = f'"$(echo "$(cat <<{tag}\n{body}\nE\n)")"'
                for home in homes:
                    assert security.is_denied(f"rm -rf {span} {home}") is not None, (body, home)
                    assert security.is_denied(f"rm -rf {nested} {home}") is not None, (body, home)
                    assert security.is_denied(f"x={nested}; rm -rf {home}") is not None, (
                        body,
                        home,
                    )
                # Direct and nested /tmp twins stay allowed, as on main.
                assert security.is_denied(f"rm -rf {span} /tmp/x") is None, body
                assert security.is_denied(f"rm -rf {nested} /tmp/x") is None, body

    def test_a_body_quote_closed_on_its_line_keeps_its_interior_quoted(self) -> None:
        """A body-line quote that CLOSES on the same line keeps the paired
        reading. Reading only its two quote characters as data (and the
        interior as live) let a body ``")"`` close the span at the body ``)``,
        and the line's closing ``"`` then opened a quote over the operand
        behind the span: ``rm -rf "$(cat <<E`` + nl + ``")"`` + nl + ``E`` +
        nl + ``)" ~`` wipes ``$HOME`` in bash. Every home/root row here
        refuses -- direct, nested, and with the ``rm`` after the span -- and
        the direct and nested ``/tmp`` twins stay allowed, as on main."""
        spans = (
            '"$(cat <<E\nE\n)"',
            '"$(cat <<E\n)\nE\n)"',
            '"$(cat <<E\n")"\nE\n)"',
            "\"$(cat <<E\n')'\nE\n)\"",
            '"$(cat <<E\n`\nE\n)"',
            "\"$(cat <<'E'\n)\nE\n)\"",
            '"$(cat <<E\nsay ")" ok\nE\n)"',
            "\"$(cat <<E\nit's ')'\nE\n)\"",
        )
        for span in spans:
            nested = f'"$(echo {span})"'
            for home in ("~", "$HOME", '"$HOME"', "${HOME}", "/"):
                assert security.is_denied(f"rm -rf {span} {home}") is not None, (span, home)
                assert security.is_denied(f"rm -rf {nested} {home}") is not None, (span, home)
                assert security.is_denied(f"x={span}; rm -rf {home}") is not None, (span, home)
                assert security.is_denied(f"echo {span} ; rm -rf {home}") is not None, (
                    span,
                    home,
                )
            assert security.is_denied(f"rm -rf {span} /tmp/x") is None, span
            assert security.is_denied(f"rm -rf {nested} /tmp/x") is None, span

    def test_the_heredoc_body_is_read_structurally_to_its_terminator(self) -> None:
        """The 6th and final round reads a heredoc body STRUCTURALLY: the ``<<``
        delimiter is parsed and every body line is skipped as DATA until the
        terminator line, then the walk resumes -- so the span's real ``)`` is
        reached whatever the body holds, and the operand after it reaches the rm
        floor. Two body classes the per-character rounds still mis-sized:

        * (c109-1) a body-line ``)`` inside a nested quoted substitution closed
          the inner span early, and an UNCLOSED opener later on the body line
          (``${x`` / ``$(y`` / ``$((1`` / ``(z`` / a backtick) then ran one
          unproven span to the end of the text, so the home operand after the
          real close was never read;
        * (c109-2) an ANSI-C ``$'a\\'b`` on a body line was read as closing on
          its line while the walk kept it open.

        Reading the body as data closes both: every home/root row refuses in a
        real wipe the parent commit allowed, and each ``/tmp`` twin stays allowed
        because the real operand is read, not synthesised. (Confirmed in real
        bash: each refused command hands ``$HOME`` or ``/`` to ``rm``.)
        """
        homes = ("~", "$HOME", '"$HOME"', "${HOME}", "/")
        openers = ("${x", "$(y", "$((1", "(z", "`w")
        for tag in ("E", "'E'"):
            # c109-1: body ``)`` + an unclosed opener, same line and next line.
            for opener in openers:
                for body in (f") {opener}", f")\n{opener}"):
                    nested = f'"$(echo "$(cat <<{tag}\n{body}\nE\n)")"'
                    for home in homes:
                        assert security.is_denied(f"rm -rf {nested} {home}") is not None, (
                            tag,
                            body,
                            home,
                        )
                        assert security.is_denied(f"x={nested}; rm -rf {home}") is not None, (
                            tag,
                            body,
                            home,
                        )
                    assert security.is_denied(f"rm -rf {nested} /tmp/x") is None, (tag, body)
            # c109-2: an ANSI-C body line, direct / nested / post-terminator.
            for abody in ("$'a\\'b", "$'\\''x", "x $'q\\'"):
                direct = f'"$(cat <<{tag}\n{abody}\nE\n)"'
                nested = f'"$(echo "$(cat <<{tag}\n{abody}\nE\n)")"'
                for home in homes:
                    assert security.is_denied(f"rm -rf {direct} {home}") is not None, (tag, abody)
                    assert security.is_denied(f"rm -rf {nested} {home}") is not None, (tag, abody)
                    assert security.is_denied(f"x={nested}; rm -rf {home}") is not None, (
                        tag,
                        abody,
                    )
                assert security.is_denied(f"rm -rf {direct} /tmp/x") is None, (tag, abody)
                assert security.is_denied(f"rm -rf {nested} /tmp/x") is None, (tag, abody)
        # Terminator tracking: a ``<<-E`` strips leading tabs from its terminator,
        # and TWO heredocs pending on one line are consumed in order, A then B. A
        # decoy ``)`` and a decoy opener in the first body are data, so the span
        # still closes at its real ``)`` and the home operand is read; the ``/tmp``
        # twin stays allowed.
        two = '"$(cat <<-A <<B\n)\t${x\n\tA\n$(y\nB\n)"'
        for home in homes:
            assert security.is_denied(f"rm -rf {two} {home}") is not None, home
        assert security.is_denied(f"rm -rf {two} /tmp/x") is None

    def test_a_single_quoted_backtick_is_literal_not_an_opener(self) -> None:
        # bash reads a backtick inside a single quote as DATA (``echo '`'``), so
        # the span matcher must not open a bogus backtick span whose body then
        # carries that ``'`` over the REAL ``$(`` after it.
        push = "bash -c 'git p\"\"ush origin main'"
        for cmd in (
            f"echo '`' \"$({push})\" '`'",
            f"echo '`' \"$(echo \"$({push})\")\" '`'",
            f"echo 'a`b' \"$({push})\" 'c`d'",
            f"echo $'`' \"$({push})\" $'`'",
            f"echo '`' <({push}) '`'",
        ):
            assert security.is_denied(cmd) is not None, cmd
        # A real (unquoted or double-quoted) backtick substitution still opens.
        assert security.is_denied(f"echo `{push}`") is not None
        assert security.is_denied(f'echo "`{push}`"') is not None
        # A single-quoted backtick beside a feature push stays allowed.
        assert security.is_denied("echo '`' \"$(date)\" '`' && git push origin feat") is None

    def test_a_bare_brace_in_a_default_is_data(self) -> None:
        from kiro_crew.security.shell_normalizer_span import _matching_close_brace

        # bash closes ``${x:-a{b}c}`` at the first ``}``
        assert _matching_close_brace("x:-a{b}c}")[:2] == (len("x:-a{b}"), True)
        assert security.is_denied('rm -rf "$(echo ${x:-{})" ~') is not None

    def test_a_heredoc_span_still_reaches_the_mint_and_kill_checks(self) -> None:
        for cmd in (
            "T=$(cat <<E\nx\nE\nprintf token); kirocrew $T",
            "kill $(cat <<E\nx\nE\npgrep kirocrew)",
        ):
            assert security.is_denied(cmd) is not None, cmd

    def test_an_arithmetic_seed_cannot_hide_a_heredoc_push(self) -> None:
        """``$((cat <<E`` is a ``$(`` holding a SUBSHELL with a real heredoc.
        Read as arithmetic, it would prove its span early and drop the
        ``bash -c '<push>'`` glued after the heredoc. Arithmetic is not
        modelled, so ``<<`` is a heredoc and the span is unproven, which scans
        the whole remainder and finds the push."""
        cmd = "echo \"$((cat <<E\n)\nE\n) ; bash -c '" + self._PUSH + "')\""
        assert security.is_denied(cmd) is not None, cmd

    def test_a_quoted_backtick_skip_keeps_the_rm_target(self) -> None:
        """The new backtick unit-skip used the QUOTE-AWARE closer, which reads a
        single-quoted backtick as data and swallows the rest of the span -- so
        ``rm_floor`` lost the ``~`` after an unproven ``$(`` and allowed a home
        wipe. The skip now uses the escape-only ``_backtick_closer`` (a single
        quote does not protect a backtick), so the span resumes at the real
        closer and the ``~`` reaches the ``rm`` segment."""
        cmd = 'rm -rf "$(echo "`echo \'`")" ~'
        assert security.is_denied(cmd) is not None, cmd

    def test_a_quoted_process_sub_in_a_default_is_data(self) -> None:
        """Inside ``${x:-"<("}`` the ``<(`` is double-quoted, so bash reads it as
        text. The brace walk opened a process sub there that never closed, so
        the span ran to the end and the rm floor lost the ``~``."""
        cmd = 'rm -rf "$(echo ${x:-"<("})" ~'
        assert security.is_denied(cmd) is not None, cmd

    def test_a_pid_dollar_opens_no_expansion(self) -> None:
        """``$$`` is the PID, so the ``{`` after it is text and the span closes
        at the ``)``. Reading the second ``$`` as a ``${`` opener ran the span to
        the end, and the rm floor lost the ``~``. A ``\\$`` is data and pairs
        with nothing, so ``\\$${`` still opens one."""
        cmd = 'rm -rf "$(echo $${)" ~'
        assert security.is_denied(cmd) is not None, cmd

        from kiro_crew.security.shell_normalizer_span import _matching_close_paren

        # After a paired ``$`` the ``{`` is data and the ``)`` proves the span;
        # after a lone one ``${)`` never closes, so the span stays unproven.
        assert [
            _matching_close_paren(f"echo {t}{{)")[1] for t in ("$", "$$", "$$$", "\\$$", "\\$$$")
        ] == [False, True, False, False, True]

    def test_an_arithmetic_seed_that_closes_a_subshell_first_is_dropped(self) -> None:
        """In ``$((a);(cat <<E`` the ``<<`` is a heredoc and its body line
        ``))`` closes nothing. Reading ``$((`` as arithmetic would take that body
        line as the arithmetic close and hide the push after it; arithmetic is
        not modelled, so the span is unproven and the push is scanned."""
        cmd = "echo \"$((a);(cat <<E\n))\nE\nbash -c 'git push origin main'))\""
        assert security.is_denied(cmd) is not None, cmd

    def test_a_quote_inside_an_earlier_span_quotes_nothing_after_it(self) -> None:
        """bash reads each span in a fresh context, so the ``'`` in
        ``"$(echo "'")"`` quotes nothing after its ``)``. A flat quote walk
        carried it out and marked the next, real ``$(`` as single-quoted."""
        cmd = 'echo "$(echo "\'")" "$(git p""ush origin main)"'
        assert security.is_denied(cmd) is not None, cmd

    def test_a_capped_span_still_scans_past_its_heredoc(self) -> None:
        """A span nested past the cap was given no extra reading. One holding a
        heredoc still has the command after the heredoc's body glued to its
        ``)``, so it is scanned from the first newline to the last ``)``."""
        deep = "$(echo " * 32 + "x" + ")" * 32
        for push in ("git push", self._PUSH):
            cmd = f"echo \"$(echo {deep}; cat <<E\n)\nE\nbash -c '{push}')\""
            assert security.is_denied(cmd) is not None, cmd

    def test_a_capped_span_that_never_closes_cannot_hide_a_later_push(self) -> None:
        """A span past the cap reads its deep interior WITHOUT fresh contexts, so
        a ``)`` that is only DATA there -- a ``${x:-(}`` default value's paren --
        is counted as a close and consumes one of the span's own ``)``. The span
        then never closes in the gate's reading, though bash balances it and runs
        the opener AFTER it as a sibling. The whole-remainder body is a SUFFIX the
        payload walk value-suppresses, so a ``$(bash -c '<push>')`` hidden behind
        the span was never a payload source and the git-publish floor never ran:
        ``is_denied`` returned None for a command bash pushes ``main`` with (Opus,
        head a22b3355b6). The never-close branch now scans the remainder once,
        emitting each later substitution segment as its own shallow entry, so the
        hidden launcher is read. Every row is a protected push bash actually runs
        (verified with a stub git); the nested-bash ``$(...)``, the backtick form
        and the plain-eval form each fail (allowed) on a22b3355b6 and pass now."""
        prefix = "echo " + "$(echo " * 33 + "${x:-(}" + ")" * 33 + " ; "
        launchers = ("bash -c", "sh -c", "eval")
        targets = ("main", "mainline")
        for sep in (" ; ", " && ", "\n"):
            pfx = "echo " + "$(echo " * 33 + "${x:-(}" + ")" * 33 + sep
            for launcher in launchers:
                for target in targets:
                    push = f"git push origin {target}"
                    for sub in (
                        f"echo \"$({launcher} '{push}')\"",  # launcher in a $() sub
                        f"echo \"`{launcher} '{push}'`\"",  # launcher in a backtick sub
                        f"$({launcher} '{push}')",  # bare $() sub
                        f"`{launcher} '{push}'`",  # bare backtick sub
                    ):
                        cmd = pfx + sub
                        assert security.is_denied(cmd) is not None, cmd
        # A direct (launcher-less) push behind the span is caught by the raw
        # floor already; it must stay denied.
        assert security.is_denied(prefix + 'echo "$(git push origin main)"') is not None
        # A never-close span hiding a HARMLESS later command must NOT be refused
        # -- the tail scan adds findings, it does not deny on its own.
        for benign in ("echo \"$(bash -c 'echo hi')\"", 'echo "`date`"', 'echo "$(ls -l)"'):
            assert security.is_denied(prefix + benign) is None, benign
        # The same span hiding a FEATURE push stays allowed.
        assert security.is_denied(prefix + "echo \"$(bash -c 'git push origin feat')\"") is None

    def test_a_capped_span_closing_late_cannot_hide_a_process_substitution(self) -> None:
        """The same data ``)`` can make the capped span close LATE instead of
        never: at the ``)`` of an unquoted PROCESS substitution bash runs as a
        sibling after its own close. The span then reads as closed, so the walk
        skipped past it and the ``<(bash -c '<push>')`` body was never a payload
        source; the ``$(bash`` spelling was caught only because a ``$(bash``
        token reads as a launcher, which ``<(bash`` does not (bolichen97, head
        d42c8f4354). The late-closing span is now scanned once for its
        substitution segments, as the never-close span is. Every row is a
        protected push bash runs (stub git); each was allowed on d42c8f4354."""
        for sep in (" ; ", " && "):
            prefix = "echo " + "$(echo " * 33 + "${x:-(}" + ")" * 33 + sep
            for target in ("main", "mainline"):
                push = f"git push origin {target}"
                for sub in (
                    f"cat <(bash -c '{push}')",
                    f"cat <(sh -c '{push}')",
                    f"cat <(eval '{push}')",
                    f"echo x > >(bash -c '{push}')",
                    f"echo x > >(sh -c '{push}')",
                ):
                    assert security.is_denied(prefix + sub) is not None, prefix + sub
            # A harmless or FEATURE process substitution behind it stays allowed.
            for allowed in ("cat <(echo done)", "cat <(bash -c 'git push origin my-feature')"):
                assert security.is_denied(prefix + allowed) is None, prefix + allowed

    def test_a_capped_span_inside_an_outer_body_cannot_hide_its_launcher(self) -> None:
        """A capped span nested inside an outer ``"$(...)"`` makes the OUTER span
        capped too. Its segment scan emitted only the text inside the NESTED
        openers, so the outer body's own ``; bash -c '<push>'`` was never a
        payload source, and at the root the double-quoted word stays one token.
        The scan starts inside the outer span, so its own text is a segment.
        Every row is a protected push bash runs (stub git); unquoted, the same
        shape was already refused."""
        nest = "$(" * 33 + ":" + ")" * 33
        for opener in ('echo "$(', 'x="$('):
            for sep in (" ; ", " && "):
                for launcher in ("bash -c", "sh -c", "eval"):
                    for target in ("main", "mainline"):
                        cmd = f"{opener}{nest}{sep}{launcher} 'git push origin {target}')\""
                        assert security.is_denied(cmd) is not None, cmd
                # The same shape with a FEATURE push or a harmless tail stays allowed.
                for tail in ("bash -c 'git push origin my-feature'", "echo done"):
                    cmd = f'{opener}{nest}{sep}{tail})"'
                    assert security.is_denied(cmd) is None, cmd

    def test_a_capped_span_closing_early_cannot_hide_its_launcher(self) -> None:
        """The data ``)`` in a ``${x:-)}`` default, read past the nesting cap,
        closes the capped span EARLY instead of late: one ``)`` too soon, so the
        span's own ``; bash -c '<push>'`` before bash's real close sat after the
        guessed close, where no opener reaches it, and at the root the
        double-quoted word stays one token. The text past a capped span's close
        is scanned once for its segments, every ``)`` there a boundary that
        closes nothing, so the launcher is read however early the guess was.
        Most of these rows push a protected branch when bash runs them (stub
        git); the rest are over-refused, the fail-closed direction."""
        for data in ("${x:-)}", "${x:-))}", "`echo )`"):
            for nest_open, nest_close in (("$(echo ", ")"), ('"$(echo ', ')"'), ("<(echo ", ")")):
                early = nest_open * 33 + data + nest_close * 33
                for opener, close in (('echo "$(', ')"'), ("echo $(", ")"), ("cat <(", ")")):
                    for sep in (" ; ", " && ", "\n"):
                        for target in ("main", "mainline"):
                            push = f"git push origin {target}"
                            for tail in (f"bash -c '{push}'", f"eval '{push}'", push):
                                cmd = f"{opener}{early}{sep}{tail}{close}"
                                assert security.is_denied(cmd) is not None, cmd
                    # A harmless tail behind the same early close stays allowed.
                    cmd = f"{opener}{early} ; echo done{close}"
                    assert security.is_denied(cmd) is None, cmd

    def test_a_proven_body_equal_to_a_source_suffix_is_re_extracted(self) -> None:
        """``echo $(x <(...) \\') 'x <(...) \\'`` has a PROVEN ``$(`` body whose
        text equals the end of its source. Suppression by VALUE (any suffix body)
        queued that body without extracting its substitutions, so its
        ``<(bash -c '<push>')`` was never read, and the later copy was a
        duplicate of a payload already seen. Only an over-read entry (an
        unproven span's remainder or tails) is suppressible; a proven body is
        always re-extracted. Every row is a protected push bash runs."""
        for spell in ("git push", 'git p""ush'):
            for sub in ("<", ">"):
                for launcher in ("bash -c", "sh -c", "eval"):
                    for target in ("main", "mainline"):
                        inner = f"{sub}({launcher} '{spell} origin {target}')"
                        cmd = f"echo $(x {inner} \\') 'x {inner} \\'"
                        assert security.is_denied(cmd) is not None, cmd
                    # The FEATURE push twin stays allowed.
                    inner = f"{sub}({launcher} '{spell} origin my-feature')"
                    assert security.is_denied(f"echo $(x {inner} \\') 'x {inner} \\'") is None
        assert security.is_denied("echo $(x <(echo done) \\') 'x <(echo done) \\'") is None

    def test_a_quoted_or_escaped_paren_cannot_end_the_capped_tail_early(self) -> None:
        """The capped tail scan ended its span at the FIRST ``)`` it read other
        than a single-quoted one, so a double-quoted ``)`` that is data
        (``echo ")"``) or an escaped ``\\)`` closed the span early and the
        ``; bash -c '<push>'`` behind it was never a segment. bash keeps the
        span open there -- a double-quoted ``)`` whose opener was not itself
        double-quoted, and an escaped ``)``, are both literal -- and closes it
        at the real ``)``, so the launcher runs as a sibling. The scan now
        treats a ``)`` as a boundary only when it is unquoted-and-unescaped, or
        a double-quoted ``)`` closing a span whose opener was double-quoted.
        Every protected row is a push bash runs (stub git)."""
        nest = "$(" * 33 + ":" + ")" * 33
        decoys = (' ; echo ")" ; ', " ; echo \\) ; ")
        for opener in ('echo "$(', 'x="$('):
            for decoy in decoys:
                for launcher in ("bash -c", "sh -c", "eval"):
                    for target in ("main", "mainline"):
                        cmd = f"{opener}{nest}{decoy}{launcher} 'git push origin {target}')\""
                        assert security.is_denied(cmd) is not None, cmd
                # A FEATURE push or a harmless tail behind the same decoy stays allowed.
                for tail in ("bash -c 'git push origin my-feature'", "echo done"):
                    cmd = f'{opener}{nest}{decoy}{tail})"'
                    assert security.is_denied(cmd) is None, cmd
        # A genuine double-quoted ``)`` that DOES close an inner double-quoted
        # ``$(`` is still a boundary, so a push after that real close is seen.
        inner = "\"$(bash -c 'git push origin main')\""
        assert security.is_denied(f'echo "$({nest} ; echo {inner})"') is not None
