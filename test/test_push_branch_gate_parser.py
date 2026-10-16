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
        # Nested substitutions: the outer body keeps the inner one whole. The
        # extractor returns ONE body per top-level opener; the payload walk is what
        # descends, so the inner command is reached from the outer body.
        assert _substitution_bodies(">(echo >(git push origin main))") == [
            "echo >(git push origin main)"
        ]
        assert "git push origin main" in security._shell_payload_sources(
            "cat >(echo >(git push origin main))"
        )
        # UNPROVEN span -> the whole remainder, the fail-closed direction.
        assert _substitution_bodies(">(git push origin main") == ["git push origin main"]
        assert _substitution_bodies(">(X=')' git push origin main") == [
            "X=')' git push origin main"
        ]

    def test_the_span_helper_is_quote_aware_and_reports_proof(self):
        from kiro_crew.security import _matching_close_paren

        # offset 2 is just past a ">(" / "$(" opener.
        assert _matching_close_paren(">(a)", 2) == (4, True)
        assert _matching_close_paren(">(a(b))", 2) == (7, True)
        # A quoted closer is data and must not end the span.
        assert _matching_close_paren(">(X=')' a)", 2) == (10, True)
        assert _matching_close_paren('>(X=")" a)', 2) == (10, True)
        assert _matching_close_paren(">(X=$')' a)", 2) == (11, True)
        # A quoted OPENER must not deepen it either.
        assert _matching_close_paren(">(X='(' a)", 2) == (10, True)
        # Never balanced -> not proven, and the span runs to the end.
        assert _matching_close_paren(">(a", 2) == (3, False)
        assert _matching_close_paren(">(X=')' a", 2) == (9, False)

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
