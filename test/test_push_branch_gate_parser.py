"""The parser underneath the git-publish branch gate, pinned layer by layer.

``test_push_branch_gate.py`` pins the gate's answers: the feature-branch allow, the
protected-branch deny, the option and abbreviation readings, and the enforcement
point's audits. This module pins the machinery those answers rest on in
``kiro_crew.security``: every branch of the positional/arity parser as a table, the
contract between the tokenizer and the argument scan, the boundary between a proven
and an unprovable process substitution, the one quote model every push-path reading
walks, and the nested-payload extractor that spans the proven boundary.
"""

import ast
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.security import is_denied

# "git pus" + "h" keeps a literal blocked command out of the test source.
PUSH = "git pus" + "h"


def _multi_line_push_corpus() -> set[str]:
    """Both gate modules' multi-line push literals plus generated head x push x tail shapes."""
    corpus = set()
    for name in ("test_push_branch_gate.py", "test_push_branch_gate_parser.py"):
        tree = ast.parse((Path(__file__).parent / name).read_text(encoding="utf-8"))
        corpus |= {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "\n" in node.value
            and "push" in node.value
            and "{}" not in node.value
        }
    heads = (
        "",
        "git commit -m 'x\n\ny'\n",
        "git push origin feat # don't\n",
        'X="\n',
        "trap 'x\n",
        "sh <<E\n",
        'echo "a\\\n',
        "cat <\\\n<EOF\n",
        'echo `echo "`\n',
    )
    pushes = (
        "git push origin main",
        "git push",
        "git push origin $b",
        "git push origin $'main'",
        "git push origin ~",
        "git push origin ${b}",
        "git push --force origin main",
        "git push origin feat",
    )
    tails = ("", " # it's", " '", ' "', "\necho '", "\nE", " #' EXIT git push origin f")
    corpus |= {h + p + t for h in heads for p in pushes for t in tails}
    assert len(corpus) > 500
    return corpus


#: Real-bash oracle rows for the inert-word allowlist: together they use every
#: ``_INERT_FLAGS`` flag and segment head, and each pushes to a protected branch
#: in real bash.
_INERT_ORACLE_ROWS = (
    "cd . && git add f && git commit -q -m 'a\n\nb' && git push -q origin main",
    "git add -- f && git commit -qm 'a\n\nb' -- f && git push -u origin main",
)

#: The commands the inert-word allowlist exists for, each allowed: the reported
#: shapes and every allowlisted flag and segment head, with feature targets.
_INERT_ALLOW_ROWS = (
    "git commit -m 'a\nb'; git push -u fork fix/x",
    'git commit -q -m "subject\n\nbody" && git push -q origin feat-x',
    "git commit -m 'fix: x\n\nbody line'; git push -u fork fix/x",
    "cd /r && git add f && git commit -m 'fix: x\n\nbody line' && git push origin docs/x",
    'cd /r && git add f && git commit -m "fix: x\n\nbody line" && git push origin docs/x',
    'git commit -m "a\n\nFixes #1" && git push origin feat-x',
    "git commit -m 'a\n\nb' -- src/x.py; git push origin f",
    "git commit -qm 'x\n\nFixes #1'; git push -u fork fix/x",
    "cd /r && git commit -m 'a\n\nb' && git push origin feat",
    *(row.replace("origin main", "origin feat") for row in _INERT_ORACLE_ROWS),
)


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

    def test_a_newline_inside_quotes_stays_in_its_word(self):
        # A multi-line commit message must not split the command mid-string: the
        # next segment's walk would restart unquoted, read the message's CLOSING
        # quote as an opener that swallows ``&&``, and turn a literal feature push
        # into one unparseable word refused as target-unverifiable.
        from kiro_crew.security import _split_push_command_segments

        assert _split_push_command_segments('git commit -m "a\n\nb" && git push origin f') == [
            'git commit -m "a\n\nb" ',
            " git push origin f",
        ]
        for cmd in (
            'git commit -q -m "subject\n\nbody" && git push -q origin feat-x',
            "git commit -m 'a\nb'; git push -u fork fix/x",
        ):
            assert is_denied(cmd) is None, cmd
        # The protected check SEES the target behind a multi-line message, and an
        # unquoted newline still separates.
        for cmd in (
            'git commit -q -m "subject\n\nbody" && git push -q origin main',
            "echo 'a\nb'\ngit push origin main",
        ):
            assert "git-publish-push-protected-branch-name" in security._git_publish_floor_tags(
                cmd
            ), cmd

    def test_a_quote_the_walk_cannot_model_keeps_the_split_at_every_newline(self):
        # The quote walk knows no comments, heredocs or substitution bodies, so a
        # quote opened inside one is a PHANTOM that bash never sees. Kept across a
        # newline it would swallow the next line, and a protected push there would
        # go unjudged. Any such construct keeps the conservative per-line split.
        for cmd in (
            "git push origin feat # it's ready\ngit push origin main",
            "git push origin feat <<EOF\ngit push origin feat '\nEOF\ngit push origin main\necho '",
            'echo "$(cat <<EOF\n"\nEOF\n)"\ngit push origin feat \'\ngit push origin main\n\'',
            'echo `echo "`\ngit push origin feat "\ngit push origin main\n"',
            # A backslash-newline is deleted by bash before it reads quotes or an
            # opener, and the per-line cut restarts the walk unquoted.
            'echo "a\\\ngit push origin feat"\ngit push origin main\necho \'"\\\'',
            "echo $\\\n'a\\'\ngit push origin feat > '\ngit push origin main\necho \\'",
            "cat <\\\n<EOF\ngit push origin feat '\nEOF\ngit push origin main\necho \\'",
            # A comment apostrophe merging a push the per-line reading flags only
            # as ungated: an ANSI-C or variable target, and one behind a real
            # multi-line quote that the per-line cut starts outside of.
            "git push origin feature # don't\ngit push origin $'main'",
            "b=main; git push origin feat # it's\ngit push origin $b\n# '",
            "git push origin feat # it's\ngit push origin ~\n# '",
            "git push origin feat # it's\nX=\"\n\" git push origin $b\n# '",
        ):
            assert security._git_publish_floor_tags(cmd), cmd
        # A ``#`` inside the quoted message is data, so the message still stays whole.
        assert is_denied('git commit -m "a\n\nFixes #1" && git push origin feat-x') is None

    def test_the_unmodelled_construct_list_is_pinned_to_the_quote_walk(self):
        # The list decides when a quoted newline is word data. Every listed
        # construct turns that trust off, so its quoted newline splits; the
        # per-line backstop covers a construct the list misses.
        from kiro_crew.security import shell_normalizer_walk as walk

        assert walk._QUOTE_WALK_UNMODELLED_RE.pattern == r"<<|\$\(|\$\{|`|[<>]\(|\\\n"
        # Every listed opener turns the trust off, so its quoted newline splits.
        for opener in ("<<", "$(", "${", "`", "<(", ">(", "\\\n"):
            assert len(walk._split_push_command_segments(f"echo {opener} 'a\nb'")) >= 2, opener
            # Inside a plain single quote bash reads it literally, so the trust stays.
            assert len(walk._split_push_command_segments(f"echo 'x{opener}\nb'")) == 1, opener
        assert is_denied("git commit -m 'fix `foo`\n\nbody'; git push origin feat") is None
        for cmd in (
            "git commit -m 'fix `foo`\n\nbody'; git push origin main",
            "git commit -m \"fix `foo`\n\nbody\"\ngit push origin feat '\ngit push origin main\n'",
        ):
            assert security._git_publish_floor_tags(cmd), cmd

    def test_a_phantom_quote_cannot_hide_a_push_even_without_the_construct_list(self, monkeypatch):
        # Defence in depth: the per-line reading judges every line that parses as
        # a push on its own, so a construct missing from the list still cannot
        # hide a protected or bare push behind a phantom quote.
        import re

        from kiro_crew.security import shell_normalizer_walk as walk

        monkeypatch.setattr(walk, "_QUOTE_WALK_UNMODELLED_RE", re.compile(r"(?!)"))
        # Each phantom quote leaves a merged segment that still parses as a push.
        for template in (
            'echo "a\\\ngit push origin feat"\n{}\necho \'"\\\'',
            "echo $\\\n'a\\'\ngit push origin feat > '\n{}\necho \\'",
            "cat <\\\n<EOF\ngit push origin feat '\nEOF\n{}\necho \\'",
        ):
            for hidden in ("git push origin main", "git push", "git push origin $b"):
                cmd = template.format(hidden)
                assert security._git_publish_floor_tags(cmd), cmd
        # The hidden line itself ends inside a phantom quote behind ungated-only
        # targets, with the comment check off too: the exemption keys on the
        # whole-text walk entering the line inside a quote, not on how it ends.
        monkeypatch.setattr(walk, "_opens_comment", lambda text, offset: False)
        for template in (
            "git push origin feat # don't\n{} # won't",
            "git commit -m 'x\n\ny'\n{} # it's\necho '",
            "git push origin feat <<E\n'\nE\n{} # don't",
            "sh <<E\n{} # don't\nE",
            "b=main; trap 'x\n{} #' EXIT git push origin feat",
        ):
            for target in ("$b", "$'main'", "~", "${b}"):
                cmd = template.format("git push origin " + target)
                assert security._git_publish_floor_tags(cmd), cmd

    def test_the_splitter_keeping_a_quoted_newline_is_what_allows_the_reported_command(
        self, monkeypatch
    ):
        # The per-line exemption alone does not allow these: with the splitter
        # told to split inside quotes again, each is refused. Both halves decide.
        from kiro_crew.security import argv_floor as af

        cmds = (
            "git commit -m 'fix: x\n\nbody line'; git push -u fork fix/x",
            "cd /r && git add f && git commit -m 'fix: x\n\nbody line' && git push origin docs/x",
            'cd /r && git add f && git commit -m "fix: x\n\nbody line" && git push origin docs/x',
        )
        for cmd in cmds:
            assert security.is_denied(cmd) is None, cmd
        split = af._split_push_command_segments
        monkeypatch.setattr(
            af,
            "_split_push_command_segments",
            lambda text, keep_quoted_newlines=True: split(text, False),
        )
        for cmd in cmds:
            assert security.is_denied(cmd) is not None, cmd

    @pytest.mark.parametrize("construct_list", [True, False])
    def test_every_per_line_deny_survives_the_whole_text_reading(self, monkeypatch, construct_list):
        # Differential against the per-line reading: each line judged alone is the
        # single-line verdict, so the whole text must keep every tag a line earns.
        # Only the ungated sentinel may go, and only on a line the whole-text walk
        # enters inside a quote. Run over this module's own multi-line push corpus
        # plus generated head x push x tail variants, with the list on and off.
        import re

        from kiro_crew.security import shell_normalizer_walk as walk

        if not construct_list:
            monkeypatch.setattr(walk, "_QUOTE_WALK_UNMODELLED_RE", re.compile(r"(?!)"))
        corpus = _multi_line_push_corpus()
        for cmd in sorted(corpus):
            whole = security._git_publish_floor_tags(cmd)
            entries = [0] + [s.state for s in walk._iter_shell_chars(cmd) if s.char == "\n"]
            for line, entry in zip(cmd.split("\n"), entries):
                if not security._is_git_publish(line):
                    continue
                lost = set(security._git_publish_floor_tags(line)) - whole
                if lost == {security._GIT_PUBLISH_UNGATED}:
                    assert entry, (cmd, line)
                else:
                    assert not lost, (cmd, line, lost)

    def test_no_command_real_bash_runs_as_a_protected_push_is_allowed(self, monkeypatch, tmp_path):
        # Oracle independent of ``_iter_shell_chars``: real bash parses and runs
        # each corpus command, read from a file, with ``git`` a shell function
        # that only logs its argv (``b=main``, ``g=git``; ``sh``/``bash`` re-enter
        # the same bash, and PATH is an empty directory). Functions and a script
        # file keep the stubs free of shebangs and symlinks, so Windows runs it
        # under the shared Git Bash lookup. Any push bash really executes to a
        # protected or bare target must be denied, with the construct list on and
        # off. A missing bash FAILS rather than skips, on every platform.
        import os
        import re

        from conftest import _find_posix_test_shell
        from kiro_crew.security import shell_normalizer_walk as walk

        bash = _find_posix_test_shell() if os.name == "nt" else shutil.which("bash")
        assert bash, "the real-bash oracle needs bash"
        stubs = tmp_path / "stubs.sh"
        stubs.write_bytes(
            b'git() { printf \'%s\\037\' "$@" >> "$LOG"; printf \'\\n\' >> "$LOG"; }\n'
            b'sh() { "$BASH_BIN" "$@"; }\nbash() { "$BASH_BIN" "$@"; }\n'
        )
        empty = tmp_path / "bin"
        empty.mkdir()
        log = tmp_path / "log"
        script = tmp_path / ".oracle-cmd"
        env = {"PATH": str(empty), "LOG": log.as_posix(), "HOME": tmp_path.as_posix(), "b": "main"}
        env.update(g="git", GIT_DIR=(tmp_path / "none").as_posix())
        env.update(BASH_ENV=stubs.as_posix(), BASH_BIN=Path(bash).as_posix())
        env.update({k: os.environ[k] for k in ("SYSTEMROOT",) if k in os.environ})
        unsafe = re.compile(r"\bcd\b(?! \. )|/git|\bcommand\b|\benv\b|PATH")
        ran_protected = []
        for cmd in sorted(c for c in _multi_line_push_corpus() if not unsafe.search(c)):
            log.write_bytes(b"")
            script.write_bytes(cmd.encode())
            subprocess.run(  # noqa: S603 - argv list, stubbed PATH, scratch cwd
                [bash, "--noprofile", "--norc", script.name],
                cwd=tmp_path,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            for row in log.read_text().splitlines():
                args = row.split("\x1f")[:-1]
                while args[:1] in (["-C"], ["-c"]):
                    args = args[2:]
                ran = " ".join(["git", *(shlex.quote(a) for a in args)]).lower()
                if args[:1] == ["push"] and security._is_push_to_protected_branch(ran):
                    ran_protected.append(cmd)
                    break
        assert len(ran_protected) > 100
        assert "trap $'\\x67it push origin main\n' EXIT; git push origin f" in ran_protected
        assert set(_INERT_ORACLE_ROWS) <= set(ran_protected)
        for walk_trust in (True, False):
            if not walk_trust:
                monkeypatch.setattr(walk, "_QUOTE_WALK_UNMODELLED_RE", re.compile(r"(?!)"))
            for cmd in ran_protected:
                assert is_denied(cmd) is not None, (walk_trust, cmd)

    def test_every_inert_allowlist_entry_has_a_real_bash_oracle_row(self):
        # Widening the inert allowlist (a flag, a segment head) needs a row in
        # ``_INERT_ORACLE_ROWS``, which the real-bash oracle above requires to run
        # a protected push that stays denied. This test reads the allowlist
        # itself, so a new entry without a row fails here.
        from kiro_crew.security import shell_normalizer_walk as walk

        flags: dict[str, set[str]] = {}
        heads: set[str] = set()
        for row in _INERT_ORACLE_ROWS:
            for segment in row.split("&&"):
                words = shlex.split(segment)
                heads.add(words[0])
                if words[0] != "git":
                    continue
                args = words[1:]
                verb, opts = args[0], args[1 : args.index("--") if "--" in args else None]
                used = flags.setdefault(verb, set())
                used |= {w.split("=", 1)[0].lower() for w in opts if w.startswith("--")}
                used |= {
                    "-" + c.lower() for w in opts if w[:1] == "-" and w[1:2] != "-" for c in w[1:]
                }
        for verb, allowed in walk._INERT_FLAGS.items():
            assert allowed <= flags.get(verb, set()), (verb, allowed - flags.get(verb, set()))
        assert heads == {"cd", "git"}

    def test_generated_mask_edge_words_never_make_a_dash_word_inert(self):
        # Mask-edge words (empty, dash-leading, glued, escaped quotes) generated
        # around, beside and glued to a multi-line commit message whose body is a
        # disguised push, then a feature push. The inert exemption may hold only
        # where every word bash passes to ``git commit`` is non-empty and none but
        # ``-m``/``-q`` leads with a dash, so no edge word can open the editor.
        edges = ("''", '""', "'-e'", '"-e"', "'-'e", "-'e'", "''-e", "-e''", "'--edit'")
        edges += ("'a'b", "a'b'", "'a'\"b\"", "'a'\\''b'", "\\'", "'a\\'", '"a\\"', "-e")
        messages = ("'x\n$1 push origin main\n'", "'x\n\ny'", '"x\n\ny"')
        shapes = ("git commit {w} -m {m}; ", "git commit -m {m} {w}; ", "git commit -m {w}{m}; ")
        allowed = 0
        for shape in shapes:
            for word in edges:
                for msg in messages:
                    commit = shape.format(w=word, m=msg)
                    if is_denied(commit + f"{PUSH} origin feat") is not None:
                        continue
                    allowed += 1
                    try:
                        words = shlex.split(commit.rstrip("; "))[2:]
                    except ValueError:  # an unclosed quote: bash runs nothing
                        continue
                    assert all(w and (w[:1] != "-" or w in ("-m", "-q")) for w in words), commit
        assert allowed > 20

    def test_quoted_text_that_quotes_a_push_line_is_judged_line_by_line(self):
        # Multi-line quoted DATA that mentions a push (printed instructions, a PR
        # comment, a commit message quoting a retired command) is one word that
        # does not parse as a push. It is read line by line, as before, so a quoted
        # feature push stays allowed and a quoted protected one stays denied.
        for cmd in (
            'echo "To publish, run:\ngit push origin my-feature\nthen open a PR"',
            "printf 'Next:\ngit push origin my-feature\nopen a PR\n'",
            'gh pr comment 1 --body "rebase, then\ngit push --force-with-lease origin fix/t\nok"',
            'git commit -m "docs: retire\nwas:\ngit push origin release-notes\ndone"',
        ):
            assert is_denied(cmd) is None, cmd
        assert security._git_publish_floor_tags('echo "run:\ngit push origin main\n"')
        # Quoted text a trap runs later is a push line that starts inside the quote,
        # and a fragment glued to the closing quote keeps the quoted word going.
        for cmd in (
            "trap 'x\ngit push origin $b #' EXIT git push origin f",
            "trap ':\n'git' push origin $b #' EXIT; git push origin f",
            "trap ':\n'\"git push origin $b #\" EXIT; git push origin f",
            "trap ':\n'$g' push origin $b #' EXIT; git push origin f",
            # A word the trap decodes or expands is code, not data, however it ends.
            "trap $'\\x67it push origin main\n' EXIT; git push origin f",
            "trap $'\\147it push origin main\n' EXIT; git push origin f",
            'g=git; trap "$g push origin main\n" EXIT; git push origin f',
            "g=git; trap '$g push origin main\n' EXIT; git push origin f",
            "echo(){ eval \"$1\"; }; echo '$g push origin main\n'; git push origin f",
            # Inert data stops being inert once its output feeds a pipe or a file.
            "echo '$1 push origin main\n' | bash -s git; git push origin f",
            "git commit -m '$1 push origin main\n' | bash -s git; git push origin f",
            "echo '$1 push origin main\n' ';' | bash -s git; git push origin f",
            "echo '$1 push origin main\n' > x; bash x git; git push origin f",
            "echo > x '$1 push origin main\n'; bash x git; git push origin f",
            "printf -v x '$1 push origin main\n'; bash -c \"$x\" -- git; git push origin f",
            "exec > x; echo '$1 push origin main\n'; bash x git; git push origin f",
            'exec >> x && echo "$1 push origin main\n"; sh x git; git push origin f',
            # ...on any line, and however the command spells printf -v or alias.
            "exec > x\necho '$1 push origin main\n'; git push origin f\nbash x git",
            "{\necho '$1 push origin main\n'; git push origin f\n} > x; bash x git",
            "printf '-v' x '$1 push origin main\n'; git push origin f; bash -c \"$x\" -- git",
            "shopt -s expand_aliases; ali\\as echo=eval\necho '$1 push origin main\n'; git push origin f",
            "shopt -s expand_aliases; {ali,x}as echo=eval\necho '$1 push origin main\n'; git push origin f",
            "shopt -s expand_aliases; a=alias; $a echo=eval\necho '$1 push origin main\n'; git push origin f",
            # ...or changes what the owner's name runs without defining it in the text.
            "source ./h.sh; echo $'\\x67it push origin main\n'; git push origin f",
            ". ./h.sh && git commit -m $'\\x67it push origin main\n' && git push origin f",
            "hash -p ./x git; git commit -m $'\\x67it push origin main\n'; git push origin f",
            "enable -n echo; echo $'\\x67it push origin main\n'; git push origin f",
            # ...or opens an editor or pager on it, or rewrites the config that picks one.
            "git config core.editor sh; git commit --edit -m $'\\x67it push origin main\n'; git push origin f",
            "git config core.editor sh; git commit -em $'\\x67it push origin main\n'; git push origin f",
            "git commit -m $'\\x67it push origin main\n'; git commit --amend; git push origin f",
            "gh config set editor sh; gh pr comment 1 -e -b $'\\x67it push origin main\n'; git push origin f",
            # ...including an option bash dequotes, so a quote cannot hide '-e' from the list.
            "git commit '-e' -n --allow-empty -m $'\\x67it push origin main\n'; git push origin f",
            "git commit ''-e -m $'\\x67it push origin main\n'; git push origin f",
            "git commit -m $'\\x67it push origin main\n' \"--edit\"; git push origin f",
            # ...however the option is spelled, since bash decodes a $'…' word before
            # the argv: \055 and \u002d are '-', cut at the \0, so each is '-e'.
            "git commit --allow-empty $'\\x2de\\0\n' -m x; git push origin f",
            "git commit --allow-empty $'\\055e\\0\n' -m x; git push origin f",
            "git commit --allow-empty $'\\u002de\\0\n' -m x; git push origin f",
            "git tag -a v1 $'\\x2de\\0\n' -m x; git push origin f",
            # ...including an escape the lowercased text misspells
            # (``\U`` reads as ``\u``), and a double-quoted word that expands ``$1``
            # or drops a backslash-newline: neither body is literal, so neither is data.
            "git commit -m x $'\\U0000002De\\0\n'; git push origin f",
            'git commit "$1\n" -m "git push origin HEAD:main"; git push origin f',
            'git commit -m x "$*\n"; git push origin f',
            'git commit -m x "\\\n-e\n"; git push origin f',
            # ...and a pathspec after ``--`` is not a flag, so a tracked file named
            # ``--no-edit`` cannot satisfy the message rule for an editor-opening amend.
            "git commit --allow-empty -m $'\\x67it push origin HEAD:main\n'; git commit --amend -- --no-edit; git push origin f",
            "git commit --amend -- --no-edit -m $'\\x67it push origin main\n'; git push origin f",
            "git commit --amend -- --no-verify -m $'\\x67it push origin main\n'; git push origin f",
            # ...and a value-taking option outside the list cannot swallow the '-m'
            # that would otherwise satisfy the message rule for an amend.
            "git -c user.name=-m commit -n --allow-empty -m $'\\x67it push origin main\n'; git commit --amend --author -m; git push origin f",
            "git commit --allow-empty -m $'\\x67it push origin main\n'; git commit --amend --date -m; git push origin f",
            # ...and a ``--`` that is the value of ``-m`` ends no option list, so an
            # editor option after it is still checked.
            "git commit -m -- -e -m 'g\\it push origin main\n'; git push origin f",
            "git commit -am -- --edit -m 'g\\it push origin main\n'; git push origin f",
            "git commit -m 'g\\it push origin main\n' --message -- -e; git push origin f",
            # Only ``git commit`` owns an inert word; any other owner reads as on ``main``.
            "echo 'a\nb'; git push origin f",
            "git tag -a v1 -m 'a\nb'; git push origin f",
            "gh pr comment 1 -b 'a\nb'; git push origin f",
            "git -c author.name=y commit -m 'a\nb'; git push origin f",
            "git -c committer.name=z commit -m 'a\nb'; git push origin f",
            # Only the flags the reported commands use are on the list; any other
            # flag or ``git -c`` setting reads as on ``main``.
            "git -c user.name='M Q' commit -m 'x\n\nFixes #1'; git push -u fork fix/x",
            "git commit --message='a\n\nb' 'src/x.py'; git push origin f",
            "git commit --quiet -m 'a\nb'; git push origin f",
            "git commit -am 'a\nb'; git push origin f",
            "git add --all && git commit -m 'a\nb' && git push origin f",
            "git commit -m 'a\nb' && git push --force-with-lease origin f",
            "git commit -m 'a\nb' && git push --set-upstream origin f",
            # A ``$'…'`` or ``$"…"`` body is decoded or translated by bash, so it
            # is never data, even with no escape in it.
            "git commit -m $'a\nb'; git push origin f",
            'git commit -m $"a\nb"; git push origin f',
        ):
            assert security._git_publish_floor_tags(cmd), cmd
        # The same word reached through ``bash -c``: the nested payload is judged too.
        nested = 'git config core.editor sh; bash -c $\'git commit "$1\\x0a" -m "git push origin HEAD:main"; git push origin f\' -- -em'
        assert is_denied(nested) is not None
        for cmd in (
            "git commit -m 'a\nb'; git push origin f",
            "git commit -m 'a\n\nb' -- src/x.py; git push origin f",
            "git commit -qm 'x\n\nFixes #1'; git push -u fork fix/x",
            "cd /r && git commit -m 'a\n\nb' && git push origin feat",
        ):
            assert is_denied(cmd) is None, cmd
        # One real-bash oracle row per allowlisted segment and flag: each runs a
        # protected push in bash and stays denied, and its
        # feature-branch twin is allowed.
        for row in _INERT_ORACLE_ROWS:
            assert is_denied(row) is not None, row
            assert is_denied(row.replace("origin main", "origin feat")) is None, row
        # Outside the allowlist the refusal says how to fix it, and the fix is allowed.
        for cmd in (
            "cd ~/r && git commit -m 'a\n\nb' && git push origin feat",
            "git commit -m 'a\n\nb' && env PATH=/x git push origin feat",
        ):
            reason = is_denied(cmd)
            assert reason is not None and "Put the push on its own line" in reason, cmd
            # The hint replaces the fusion reason: this command fuses nothing.
            assert "fuses text" not in reason, cmd
        assert is_denied("git commit -m 'a\n\nb'\nenv PATH=/x git push origin feat") is None
        assert is_denied("cd ~/r && git commit -m 'a\n\nb'\ngit push origin feat") is None
        # With no line entered inside a quote, or when the push on its own line is
        # still refused, the advice cannot apply, so it is left out.
        for cmd in (
            "git add .\ngit push origin $b",
            "b=x\ngit push origin {a,main}",
            "git commit -m 'a\nb'; git push origin $b",
            "git commit -m 'a\nb' && git push origin {a,main}",
            # The floor also reads every nested payload, so a ``bash -c`` push that
            # stays unverifiable on its own line keeps the advice out.
            "bash -c 'git push origin $b'; git commit -m 'a\nb' && git push origin f",
            "git commit -m 'a\nb' && git push origin feat; bash -c 'git push origin $b'",
            "eval 'git push origin {a,main}'; git commit -m 'a\n\nb' && git push origin f",
            # A separator inside the quoted message is text: moving the push after
            # it would leave the same refusal in every resubmission.
            "git commit -m 'a; git push origin f\n\nb' && git push origin feat",
        ):
            reason = is_denied(cmd)
            assert reason is not None and "its own line" not in reason, cmd
            assert "fuses text into the push target" in reason, cmd

    def test_the_own_line_hint_is_left_out_when_the_rewrite_cannot_be_read(self, monkeypatch):
        # The hint is advice: when the payload walk fails on the rewritten command,
        # nothing shows the fix works, so the refusal keeps the substitution reason.
        from kiro_crew.security import argv_floor as af

        cmd = "cd ~/r && git commit -m 'a\n\nb' && git push origin feat"
        assert "Put the push on its own line" in (is_denied(cmd) or "")
        walk = af._shell_payload_sources

        def _fails_on_the_rewrite(text):
            if text != cmd.lower():
                raise RuntimeError("walk failed")
            return walk(text)

        monkeypatch.setattr(af, "_shell_payload_sources", _fails_on_the_rewrite)
        reason = is_denied(cmd)
        assert reason is not None and "its own line" not in reason
        assert "fuses text into the push target" in reason

    @pytest.mark.parametrize("prefix", ["", "Running: "])
    @pytest.mark.parametrize("cmd", _INERT_ALLOW_ROWS)
    def test_the_shell_title_gets_the_verdict_of_the_command_it_displays(self, prefix, cmd):
        # kiro-cli titles a shell call ``Running: <command>`` and the deny tiers
        # judge that title too, so the inert proof reads through ONE exact
        # leading prefix: otherwise the live call is refused for an allowed command.
        assert is_denied(prefix + cmd) is None, prefix + cmd

    def test_the_shell_title_prefix_is_read_through_once_and_only_exactly(self):
        # A prefixed protected or bare push is refused as the bare one is, and the
        # prefixed title is never weaker than its command over the whole corpus.
        for row in _INERT_ORACLE_ROWS:
            assert is_denied("Running: " + row) is not None, row
        for cmd in sorted(_multi_line_push_corpus()):
            if is_denied(cmd) is not None:
                assert is_denied("Running: " + cmd) is not None, cmd
        # Only one exact leading ``running: `` is read through: a second prefix, one
        # without its space, a lookalike or one mid-text is a command word.
        for cmd in _INERT_ALLOW_ROWS:
            for spelled in (
                "Running: Running: " + cmd,
                "Running:" + cmd,
                "xRunning: " + cmd,
                "Running " + cmd,
                "cd /r && Running: " + cmd,
            ):
                assert is_denied(spelled) is not None, spelled
        assert is_denied(f"git commit -m 'a\nb'; Running: {PUSH} -u fork fix/x") is not None

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
