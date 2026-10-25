"""Tests for the self-kill floor: the kill rule matches the kill's target.

A command boundary ends the argv a kill reads, the target is resolved through quoting,
nested payloads and substitutions, and the bare-``kill`` argv and raw windows survive quoted
decoys and are also searched in a de-quoted view.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import (
    _HYPH,
    _K,
    _KA,
    _NAME,
    _PK,
    _RULE_KILL,
    _RULE_MINT,
    _TOK,
    Q,
    _denied_by,
)

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


def _named(origin, *cases):
    """Lead each case with the test name it came from, so the old name stays in its id."""
    return [(origin, *c) if isinstance(c, tuple) else (origin, c) for c in cases]


class TestSelfProtectionCommandBoundaries:
    @pytest.mark.parametrize(
        "command",
        [
            "awk '{print $1}'\ncat \"$KIROCREW_SCRATCH/log\"\ngrep -E PASS report",
            "printf x | awk '{print $1}'\n"
            'python3 verify/audit_logs.py > "$KIROCREW_SCRATCH/audit.txt"\n'
            'grep -E "PASS|FAIL" "$KIROCREW_SCRATCH/audit.txt"',
            "awk '{print $1}'\r\ncat \"$KIROCREW_SCRATCH/log\"\r\ngrep -E PASS report",
            "bash -c \"printf x | awk '{print \\$1}'\n"
            'cat \\"\\$KIROCREW_SCRATCH/log\\"\ngrep -E PASS report"',
        ],
    )
    def test_later_command_is_not_an_awk_argument(self, command):
        # shlex removes an unquoted newline. Losing that boundary made the
        # formatting program look like a dynamic kill command targeting the
        # later command's scratch path.
        assert _denied_by(command) is None

    @pytest.mark.parametrize(
        "command",
        [
            f"printf x | awk '{{print $1}}'\n{_PK} -f {_NAME}",
            f"printf x | awk '{{print $1}}'\n{_K} $(pgrep -f {_NAME})",
            f"{_PK} \\\n-f {_NAME}",
            f"{_PK} -f 'first\n{_NAME}'",
            f'{_PK} -f "first\n{_NAME}"',
            f'{_PK} -f "$x\n{_NAME}"',
            f"P={_PK}\n$P -f {_NAME}",
        ],
    )
    def test_real_kill_keeps_its_target(self, command):
        assert _denied_by(command) == _RULE_KILL


class TestSelfProtectionKillTargetScoping:
    """The kill rule matches the kill TARGET, not co-occurrence.

    ``pkill``/``killall`` select processes by name, so the product name as an
    argument to them is the target.  Bare ``kill`` takes PIDs and can only aim
    at the product through a command substitution that resolves the name.

    Scoping is ARGV-STRUCTURAL: the command is tokenized (resolving shell
    quoting) before matching, rather than having its raw text split on
    separators.  ``pkill -f`` takes an extended regex and accepts a path, so
    ``pkill -f 'x|<name>'``, ``pkill -f '[;]*<name>'`` and
    ``pkill -f /usr/local/bin/<name>`` are all real by-name kills that any
    matcher reading those quoted characters as shell syntax would let through.
    """

    # --- by-name kills: still blocked ---

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            *_named(
                "test_name_targeted_kill_still_blocked",
                f"{_PK} -f {_NAME}",
                f"{_KA} {_NAME}",
                f"sudo {_KA} -9 {_NAME}",
                f"{_PK} -9 -f '{_NAME} gateway'",
                f"{_PK} {_HYPH}",
                f"{_PK} -f /usr/local/bin/{_NAME}",
                f"{_KA} -9 {_NAME} > /dev/null",
                f"{_K} -9 $(pgrep {_NAME})",
                f"{_K} $(pgrep -f '{_NAME} gateway')",
                f"{_K} $(pidof {_NAME})",
                f"{_K} $(cat /var/run/{_NAME}.pid)",
                f"{_K} `pgrep {_NAME}`",
            ),
            # `pkill -f` / `pgrep -f` take an ERE, so a `|` inside a QUOTED argument
            # is part of the target, not a shell pipe.  Treating it as a segment
            # boundary would let a by-name gateway kill through.
            *_named(
                "test_quoted_regex_alternation_is_still_a_kill",
                f"{_PK} -f 'x|{_NAME}'",
                f"{_K} $(pgrep -f 'x|{_NAME}')",
            ),
            # `[;]*` matches the empty string, so these are working by-name kills.
            # Any matcher that reads the QUOTED `;` `&` `#` `>` as shell syntax stops
            # scanning before the name and lets the kill through — the reason
            # enforcement tokenizes the command (resolving quotes) before matching
            # rather than splitting its raw text.
            *_named(
                "test_quoted_metacharacter_in_target_is_still_a_kill",
                f"{_PK} -f '[;]*{_NAME}'",
                f'{_PK} -f "[;]*{_NAME}"',
                f"{_PK} -f '[&]{_NAME}'",
                f"{_PK} -f '#{_NAME}'",
                f"{_PK} -f '>{_NAME}'",
                f"{_KA} '{_NAME};'",
            ),
            # Same class as the mint's nested-payload case: the outer tokenization
            # leaves `pkill -f <name>` as one opaque token, so the payload's own argv
            # has to be checked.
            *_named(
                "test_nested_shell_payload_is_still_a_kill",
                f'bash -c "{_PK} -f {_NAME}"',
                f'sh -c "{_KA} {_NAME}"',
                f'bash -c "{_K} $(pgrep -f {_NAME})"',
                f"bash -c \"{_PK} -f '[;]*{_NAME}'\"",
            ),
            # The kill program itself may come from an expansion.  Comparing a raw
            # `os.path.basename` sees `$(which` / `pkill)` and matches neither, so the
            # program name is normalized (wrappers stripped) before comparison — the
            # same normalization the CLI-name check already used.
            *_named(
                "test_substitution_produced_kill_program_is_still_a_kill",
                f"$(which {_PK}) -f {_NAME}",
                f"$(which {_PK}) -f '[;]*{_NAME}'",
                f"`which {_KA}` {_NAME}",
                f'"$(command -v {_PK})" -f {_NAME}',
            ),
            # Same interleaving as the mint's case, on the kill side: a redirect glued
            # to a substitution-produced program name leaves the closing paren mid-word,
            # so the program name is only recovered by peeling the layers to a fixed
            # point rather than once in a fixed order.
            *_named(
                "test_substitution_produced_kill_program_with_attached_redirect",
                f"$(which {_PK})>/tmp/out -f {_NAME}",
                f"`which {_KA}`>/tmp/out {_NAME}",
                f'"$(command -v {_PK})">/tmp/out -f {_NAME}',
            ),
            # The verb is matched on TOKENS, not on raw text: a pattern anchored on
            # preceding separators sees the `/` of an absolute path and misses it.
            *_named(
                "test_path_qualified_kill_is_still_a_kill",
                f"/usr/bin/{_K} $(pgrep -f '[;]*{_NAME}')",
                f"/bin/{_K} $(pgrep -f {_NAME})",
                f"$(which {_K}) $(pidof {_NAME})",
                f"/usr/bin/{_K} -9 $(pgrep {_NAME})",
            ),
            # An EMPTY substitution expands to nothing, so `p$()kill` runs `pkill` -- the
            # same glue-evasion as `ca""t` -> `cat`, but spelled with a substitution and
            # placed MID-WORD where a prefix-only strip never sees it.
            *_named(
                "test_empty_substitution_glue_is_still_a_kill",
                f"p$(){_K} -f {_NAME}",
                f"p``{_K} -f {_NAME}",
                f"{_K}$()all {_NAME}",
                f"$(){_PK} -f {_NAME}",
            ),
            # The glue-evasion can sit in the TARGET as well as the program name.
            *_named(
                "test_empty_substitution_inside_the_target_is_still_a_kill",
                f"{_PK} -f kiro$()crew",
                f"{_PK} -f kiro``crew",
                f"{_KA} kiro$()crew",
            ),
        ],
    )
    def test_kill_spellings_that_name_the_cli_are_kills(self, origin, cmd):
        assert _denied_by(cmd) == _RULE_KILL

    def test_nested_shell_payload_bare_kill_allowed(self):
        # Descending must not widen: a bare PID kill inside a payload is still
        # not a self-kill.
        assert _denied_by(f'bash -c "{_K} 8123"') is None

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            # Widening the verb match must not widen the TARGET match: the name still
            # has to appear inside the substitution that resolves the PID.
            *_named(
                "test_path_qualified_kill_incidental_mention_allowed",
                f"/usr/bin/{_K} 8123",
                f"/usr/bin/{_K} $(cat /tmp/pids)",
                f"/usr/bin/{_K} $(cat /tmp/pids) && cp /tmp/bk/{_NAME}.json ~/",
            ),
            # The target scan stops at the end of the kill's OWN argv, so an unrelated
            # later command that merely names the product is not swept in.  Each argument
            # is checked for the target BEFORE the boundary test, because the target may
            # itself be a quoted pattern containing a separator character.
            *_named(
                "test_kill_of_something_else_then_a_mention_allowed",
                f"{_PK} -f other; echo {_NAME}",
                f"{_PK} -f other && ls /workplace/alice/{_NAME}-wt-x",
            ),
            *_named(
                "test_incidental_mention_allowed",
                f"{_K} 12345",
                f"{_K} 12345 && cp /tmp/bk/{_NAME}.json ~/.kiro/agents/",
                f"{_K} $PID; diff /tmp/bk/{_NAME}.json ~/.kiro/agents/{_NAME}.json",
                f"{_K} $PID  # stop the stray {_NAME} preview instance",
                f"{_K} 12345 | tee /tmp/{_NAME}-gw.log",
                f"{_K} $(cat /tmp/pids) && cp /tmp/bk/{_NAME}.json ~/",
            ),
        ],
    )
    def test_kill_that_only_mentions_the_cli_allowed(self, origin, cmd):
        assert _denied_by(cmd) is None

    def test_glued_control_operator_kill_still_blocked(self):
        assert _denied_by(f"true&&{_PK} -f {_NAME}") == _RULE_KILL

    def test_glued_control_operator_bare_kill_allowed(self):
        assert _denied_by(f"true;{_K} 8123") is None

    def test_kill_nesting_deeper_than_any_cap_still_blocked(self):
        # Same structural guarantee as the mint's deep-nesting case.
        inner = f"{_PK} -f {_NAME}"
        for _ in range(5):
            inner = "bash -c " + repr(inner)
        assert _denied_by(inner) == _RULE_KILL

    def test_empty_substitution_glue_is_still_a_mint(self):
        assert _denied_by(f"kiro$()crew {_TOK}") == _RULE_MINT

    @pytest.mark.parametrize(
        ("origin", "cmd", "rule"),
        [
            # The name is assigned to a variable and invoked through the expansion, so
            # neither half alone looks dangerous.  Assignment and use are in the SAME
            # command text, so the literal is substituted back before comparison.  Only
            # literal right-hand sides are tracked -- the ambient environment is not
            # modelled, and does not need to be: the attacker supplies both halves.
            *_named(
                "test_variable_expanded_invocation_still_blocked",
                (f"X={_NAME}; $X {_TOK}", _RULE_MINT),
                (f"X={_NAME}; ${{X}} {_TOK}", _RULE_MINT),
                (f"X={_NAME}; $X>/tmp/x {_TOK}", _RULE_MINT),
                (f"P={_PK}; $P -f {_NAME}", _RULE_KILL),
            ),
            # `$'...'` and `$"..."` are quoting forms, so the `$` left behind once the
            # quotes come off is not part of the program name.
            *_named(
                "test_ansi_c_quoted_program_still_blocked",
                (f"$'{_NAME}' {_TOK}", _RULE_MINT),
                (f'$"{_NAME}" {_TOK}', _RULE_MINT),
                (f"$'{_PK}' -f {_NAME}", _RULE_KILL),
            ),
        ],
    )
    def test_expanded_program_name_is_judged_by_its_rule(self, origin, cmd, rule):
        assert _denied_by(cmd) == rule

    @pytest.mark.parametrize(
        ("origin", "cmd", "rule"),
        [
            # The value is PRODUCED by a substitution, so there is no literal to carry
            # forward.  It is resolved conservatively instead: if the substitution names a
            # protected program anywhere, the variable is treated as holding that name.
            # Over-approximating is the safe direction -- the value only matters when the
            # variable is later used AS a program, where a wrong guess is a refusal.
            *_named(
                "test_computed_assignment_value_still_blocked",
                ("X=$(printf {n}); $X {v}", _RULE_MINT),
                ("X=`printf {n}`; $X {v}", _RULE_MINT),
                ("X=$(which {n}); $X {v}", _RULE_MINT),
                ("P=$(printf PKILL); $P -f {n}", _RULE_KILL),
            ),
            # `${!V}` is INDIRECT -- it expands to the value of the variable NAMED by `V`, so
            # resolving it takes two hops through the same assignment table.
            *_named(
                "test_indirect_expansion_resolved",
                ("N=PKILL; V=N; ${{!V}} -f {n}", _RULE_KILL),
                ("A={n}; B=A; ${{!B}} {v}", _RULE_MINT),
                ("x=p; x=${{x}}kill; y=x; ${{!y}} -f {n}", _RULE_KILL),
            ),
            # `${K:0}` / `${K^^}` / `${K/x/y}` transform the variable's OWN value, so none of
            # them is a plain `${K}`.  Resolved to the value itself: the transformation is not
            # modelled, and over-approximating is the safe direction, because the result only
            # matters where it is used as a program or a verb.
            *_named(
                "test_parameter_transformation_on_a_tracked_variable",
                ("K={n}; ${{K:0}} {v}", _RULE_MINT),
                ("K={n}; ${{K:0:9}} {v}", _RULE_MINT),
                ("K={n}; ${{K^^}} {v}", _RULE_MINT),
                ("K={n}; ${{K/x/y}} {v}", _RULE_MINT),
                ("K={n}; ${{K#z}} {v}", _RULE_MINT),
                ("K=PKILL; ${{K:0}} -f {n}", _RULE_KILL),
                ("V2={v}; {n} ${{V2:0}}", _RULE_MINT),
            ),
            # `bash<<<'<payload>'` glues program, operator and payload into ONE token, so the
            # program never appears as a token of its own; the operator is split off instead.
            *_named(
                "test_glued_herestring",
                ("bash<<<'{n} {v}'", _RULE_MINT),
                ("sh<<<'{n} {v}'", _RULE_MINT),
                ("bash<<<'PKILL -f {n}'", _RULE_KILL),
            ),
            # `a=(<name> <verb>); "${a[@]}"` runs the elements AS a command line.  The
            # expansion is a single token, so there are no adjacent operands for the argv
            # checks -- the joined elements go to the payload walk, which re-tokenizes them.
            *_named(
                "test_bash_array_expanded_as_a_command",
                ('a=({n} {v}); "${{a[@]}}"', _RULE_MINT),
                ("a=({n} {v}); ${{a[*]}}", _RULE_MINT),
                ('arr=({n} {v}); "${{arr[@]}}"', _RULE_MINT),
                ('a=({n} {v}); echo hi; "${{a[@]}}"', _RULE_MINT),
                ('a=(PKILL -f {n}); "${{a[@]}}"', _RULE_KILL),
                ('a=(killall {n}); "${{a[@]}}"', _RULE_KILL),
            ),
            # `$SHELL -c '<payload>'` runs the payload exactly as a named shell does; the
            # recognizer already used for the `| $SHELL` evaluator sink applies here too.
            *_named(
                "test_shell_reached_through_a_variable_is_a_nested_shell",
                ("$SHELL -c '{n} {v}'", _RULE_MINT),
                ("${{SHELL}} -c '{n} {v}'", _RULE_MINT),
                ("$SHELL -c 'PKILL -f {n}'", _RULE_KILL),
            ),
        ],
    )
    def test_resolved_program_text_is_judged_by_its_rule(self, origin, cmd, rule):
        text = cmd.format(n=_NAME, v=_TOK).replace("PKILL", _PK)
        assert _denied_by(text) == rule

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            *_named(
                "test_computed_assignment_without_a_protected_name_allowed",
                "X=$(date); echo $X {v}",
                "X=$(cat /workplace/alice/{n}-wt-x/f); echo $X {v}",
            ),
            # Expandability is the test, not the mere presence of a glob: `kiro[x]few` cannot
            # expand to the CLI, `ls` is not an invocation of it, and `echo` treats it as data.
            *_named(
                "test_glob_that_cannot_name_the_cli_allowed",
                "kiro[x]few {v}",
                "ls ./bin/kiro*rew",
                "echo kiro[c]rew {v}",
            ),
            *_named(
                "test_ordinary_text_processing_still_allowed",
                "awk '{{print $1}}' /workplace/alice/{n}-wt-x/log",
                "awk '/{v}/ {{print}}' /workplace/alice/{n}-wt-x/log",
                "sed -n '1,5p' /workplace/alice/{n}-wt-x/README.md",
            ),
        ],
    )
    def test_text_that_cannot_name_the_cli_allowed(self, origin, cmd):
        assert _denied_by(cmd.format(n=_NAME, v=_TOK)) is None

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            # A guard on the data-consumer exemption itself: `shlex` attributes
            # `foo;<name>` to the PRECEDING `echo`, while the part after the operator is a
            # new command that really runs.  Inheriting the exemption there would turn
            # the exemption into a bypass.
            *_named(
                "test_data_consumer_exemption_does_not_cross_a_glued_operator",
                f"echo foo;{_NAME}>/tmp/x {_TOK}",
                f"echo foo;{_NAME} {_TOK}",
                f"echo foo;{_PK}>/tmp/x -f {_NAME}",
                f"echo foo&&{_PK} -f {_NAME}",
            ),
            # A second guard on the same exemption: `echo … | sh` produces the
            # dangerous command as TEXT and then hands it to something that runs it, so
            # "arguments are just data" does not hold -- the data IS the command.  The
            # printed text is therefore re-tokenized as a payload too.
            *_named(
                "test_data_consumer_exemption_refused_when_piped_to_a_shell",
                f"echo {_NAME} {_TOK} | sh",
                f"echo {_NAME} {_TOK} | bash",
                f"echo '{_PK} -f {_NAME}' | sh",
                f"echo {_NAME} {_TOK} | xargs sh -c",
            ),
        ],
    )
    def test_data_consumer_exemption_does_not_cover_a_real_run(self, origin, cmd):
        assert _denied_by(cmd) is not None

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            # Piping into `$SHELL` runs the piped text exactly as piping into `bash` does,
            # and the expansion hides the program name from any basename comparison.  The
            # variables that conventionally hold a shell are recognised as evaluators.
            *_named(
                "test_variable_expanded_shell_sink_still_blocked",
                "echo 'PKILL -f {n}' | $SHELL",
                "echo 'PKILL -f {n}' | ${{SHELL}}",
                "echo {n} {v} | $SHELL",
                'echo {n} {v} | "$SHELL"',
            ),
            # A herestring feeds the script on STDIN rather than as an argument, so its text
            # is a command just as a `-c` argument is.  Both the spaced and glued spellings
            # are covered.  (A heredoc was already caught -- its newline puts the name in
            # command position for the raw-text half.)
            *_named(
                "test_herestring_payload_still_blocked",
                "bash <<< '{n} {v}'",
                'bash <<< "{n} {v}"',
                "sh <<< '{n} {v}'",
                "bash <<< '{n} >/tmp/x {v}'",
                "bash <<< 'PKILL -f {n}'",
            ),
        ],
    )
    def test_text_fed_to_a_shell_still_blocked(self, origin, cmd):
        text = cmd.format(n=_NAME, v=_TOK).replace("PKILL", _PK)
        assert _denied_by(text) is not None

    @pytest.mark.parametrize(
        "cmd",
        [
            f"if true; then {_NAME} {_TOK}; fi",
            f"if true; then {_NAME} {_TOK}; else echo no; fi",
            f"({_NAME} {_TOK})",
            f"while :; do {_NAME} {_TOK}; done",
        ],
    )
    def test_verb_carrying_its_own_boundary_still_blocked(self, cmd):
        # A shell construct hands the verb over as `<verb>;` or `<verb>)` -- ONE token
        # that both IS the verb and carries the boundary.  The verb is therefore
        # normalized and compared BEFORE the boundary test, or the argument naming it
        # would be discarded as a separator.
        assert _denied_by(cmd) == _RULE_MINT

    @pytest.mark.parametrize(
        ("origin", "cmd", "rule"),
        [
            # The shell expands a glob in the program name BEFORE exec, so a literal
            # comparison never sees the real program.  The glob is translated to a regex
            # (`[...]`/`?` -> one char, `*` -> any run) and tested for whether it COULD name
            # the target.
            *_named(
                "test_globbed_program_name_still_blocked",
                ("./bin/kiro[c]rew {v}", _RULE_MINT),
                ("kiro?rew {v}", _RULE_MINT),
                ("kiro*rew {v}", _RULE_MINT),
                ("/usr/local/bin/kiro[c]rew {v}", _RULE_MINT),
                ("p[k]ill -f {n}", _RULE_KILL),
            ),
            # A value can be built FROM an already-tracked variable.  Expanding before
            # classifying is what makes the result a literal at all: left unexpanded it looks
            # computed, the earlier binding stays, and the reassignment is silently ignored.
            *_named(
                "test_name_assembled_across_assignments",
                ("x=p; x=${{x}}kill; $x -f {n}", _RULE_KILL),
                ("x=pk; y=${{x}}ill; $y -f {n}", _RULE_KILL),
                ("a=kiro; b=$a; c=${{b}}crew; $c {v}", _RULE_MINT),
                ("n=kiro; n=${{n}}crew; $n {v}", _RULE_MINT),
            ),
        ],
    )
    def test_program_name_built_by_expansion_is_judged_by_its_rule(self, origin, cmd, rule):
        assert _denied_by(cmd.format(n=_NAME, v=_TOK)) == rule

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            # A brace group expands to the real name before exec, so it is treated like any
            # other glob: translated to a regex and tested for whether it COULD name the
            # target.  `kiro{{x,y}}few` cannot, and stays allowed.
            *_named(
                "test_brace_expansion_in_program_name_blocked",
                "kiro{{c..c}}rew {v}",
                "kiro{{c,c}}rew {v}",
                "p{{k,k}}ill -f {n}",
            ),
            # `awk` has `system()` and pipe-to-command; GNU `sed` has the `e` flag.  The
            # exemption is withdrawn PER COMMAND when the script carries such a construct,
            # rather than dropping the tool from the list -- which would refuse ordinary use.
            *_named(
                "test_script_that_executes_is_not_a_data_consumer",
                "awk 'BEGIN {{ system(ARGV[1] \" \" ARGV[2]) }}' {n} {v}",
                "awk 'BEGIN {{ print | \"{n} {v}\" }}'",
                "sed 's/x/{n} {v}/e' /tmp/f",
            ),
            # The command-position path class spans a drive letter and backslashes, and
            # the argv literal admits an executable suffix (`.exe`) before the closing quote.
            # It stops at `-`, so a product-named worktree path is still not a match.
            *_named(
                "test_windows_drive_qualified_path_blocked",
                "C:\\Users\\runner\\Scripts\\{n}.exe {v}",
                "C:/Users/runner/Scripts/{n}.exe {v}",
            ),
        ],
    )
    def test_expanded_or_executing_spelling_blocked(self, origin, cmd):
        assert _denied_by(cmd.format(n=_NAME, v=_TOK)) is not None

    def test_brace_expansion_that_cannot_name_the_cli_allowed(self):
        assert _denied_by("kiro{x,y}few " + _TOK) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            'asyncio.create_subprocess_shell("{n} {v}")',
            'await create_subprocess_shell("{n} {v}")',
            'asyncio.create_subprocess_exec("{n}", "{v}")',
        ],
    )
    def test_asyncio_subprocess_sink_is_a_mint(self, cmd):
        # `asyncio.create_subprocess_shell` EXECUTES its argument exactly as `os.system`
        # does, so it belongs in the sink alternation.  The `asyncio.` prefix is optional
        # because `from asyncio import create_subprocess_shell` reaches the bare name.
        text = "python -c '" + cmd.format(n=_NAME, v=_TOK) + "'"
        assert _denied_by(text) == _RULE_MINT + "-argv"

    @pytest.mark.parametrize(
        "cmd",
        [
            'asyncio.create_subprocess_shell("PKILL -f {n}")',
            'create_subprocess_exec("PKILL", "-f", "{n}")',
        ],
    )
    def test_asyncio_subprocess_sink_can_kill(self, cmd):
        text = "python -c '" + cmd.format(n=_NAME).replace("PKILL", _PK) + "'"
        assert _denied_by(text) == _RULE_KILL + "-interpreter"

    @pytest.mark.parametrize(
        "payload,rule",
        [
            ("{n}\\040{v}", _RULE_MINT),
            ("{n}\\x20{v}", _RULE_MINT),
            ("{n}\\11{v}", _RULE_MINT),
            ("\\x6birocrew {v}", _RULE_MINT),
            ("PKILL -f\\040{n}", _RULE_KILL),
        ],
    )
    def test_printf_numeric_escape_decoded(self, payload, rule):
        # `\040` and `\x20` are both a SPACE, so leaving them literal reopens the same
        # separator gap the NAMED escapes closed -- and `\x6b` can spell a character of the
        # program name itself.  The payload is compared as the shell will actually run it.
        text = "printf '" + payload.format(n=_NAME, v=_TOK).replace("PKILL", _PK) + "' | bash"
        assert _denied_by(text) == rule

    def test_printf_escape_to_something_harmless_allowed(self):
        # Decoding is not itself suspicion: an escape in an unrelated payload is fine.
        assert _denied_by("printf 'hello\\040world' | bash") is None

    @pytest.mark.parametrize(
        "cmd",
        [
            "{n}>/tmp/x {v};echo ok",
            "{n} {v};echo ok",
            "{n} {v}&echo ok",
            "{n} {v}|tee /tmp/x",
        ],
    )
    def test_glued_control_operator_after_the_verb(self, cmd):
        # A control operator is a word BOUNDARY, not a trailing nuisance: the shell passes
        # `<verb>` and starts a new command, so an operand is truncated at the first one
        # rather than stripped from the end.
        assert _denied_by(cmd.format(n=_NAME, v=_TOK)) == _RULE_MINT

    @pytest.mark.parametrize(
        "cmd",
        [
            "kill $(echo x >/dev/null; pgrep {n})",
            "kill $(echo x; pgrep -f {n})",
            "kill $(true && pgrep {n})",
        ],
    )
    def test_separator_inside_a_substitution_does_not_end_the_argv(self, cmd):
        # `kill $(echo x; pgrep <name>)` is ONE argument -- the `;` belongs to the
        # substitution.  The scan tracks substitution depth and ends the argv only at
        # depth zero, so the half that names the target is still seen.
        assert _denied_by(cmd.format(n=_NAME)) == _RULE_KILL

    def test_substitution_belonging_to_another_command_still_allowed(self):
        # The depth tracking must not re-associate a LATER command's substitution with
        # this kill: `kill 123` and the `echo` are separate commands.
        assert _denied_by(f"kill 123; echo $(cat /tmp/{_NAME})") is None

    @pytest.mark.parametrize(
        ("origin", "payload", "rule"),
        [
            # `import subprocess as sp` makes `sp.run` the same call, and
            # `from subprocess import run` makes the bare name reachable, so the module
            # qualifier on a sink is any identifier or absent.
            *_named(
                "test_sink_module_alias_is_the_same_sink",
                ("import subprocess as sp; sp.run('{n} {v}', shell=True)", _RULE_MINT),
                ("from subprocess import run; run('{n} {v}', shell=True)", _RULE_MINT),
                ("import subprocess as sp; sp.Popen('PKILL -f {n}', shell=True)", _RULE_KILL),
            ),
            # `subprocess.getoutput` RUNS the command and returns its output.  The catalog
            # carried the Python 2 `commands.getoutput` spelling but not the modern one.
            *_named(
                "test_subprocess_output_sinks",
                ("subprocess.getoutput('{n} {v}')", _RULE_MINT),
                ("subprocess.getstatusoutput('{n} {v}')", _RULE_MINT),
                ("from subprocess import getoutput; getoutput('{n} {v}')", _RULE_MINT),
                ("sp.getoutput('PKILL -f {n}')", _RULE_KILL),
            ),
        ],
    )
    def test_python_sink_spellings_are_sinks(self, origin, payload, rule):
        body = payload.format(n=_NAME, v=_TOK).replace("PKILL", _PK)
        text = "python -c " + Q + body + Q
        expected = rule + ("-interpreter" if rule == _RULE_KILL else "-argv")
        assert _denied_by(text) == expected

    @pytest.mark.parametrize(
        "cmd",
        [
            "P=$(PGREP {n}); kill $P; echo done",
            "P=$(PGREP -f {n}); kill -9 $P",
        ],
    )
    def test_pids_computed_from_our_own_name(self, cmd):
        # `kill` takes PIDs, so a bare name is not something a person types -- it gets
        # there by expansion, and the expansion that produced it was a lookup of our own
        # processes.  An operand of the kill's OWN argv that resolves to the name counts.
        text = cmd.format(n=_NAME).replace("PGREP", "p" + "grep")
        assert _denied_by(text) == _RULE_KILL

    def test_pids_computed_from_another_name_allowed(self):
        assert _denied_by("P=$(" + "p" + "grep nginx); kill $P") is None

    def test_kill_with_the_name_belonging_to_another_command_allowed(self):
        # The operand scan is scoped to the kill's own argv: here the name is an operand
        # of `cp`, which is why this everyday command stays allowed.
        assert _denied_by(f"kill 8123 && cp /tmp/{_NAME}.json ~/") is None

    def test_computed_mint_verb(self):
        # `T=$(printf <verb>); <name> $T` computes the VERB rather than the program.
        assert _denied_by(f"T=$(printf {_TOK}); {_NAME} $T") == _RULE_MINT

    @pytest.mark.parametrize(
        ("origin", "payload", "rule"),
        [
            # An interpreter binds the halves to its OWN variables and then uses the names.
            # Inlining those bindings is the interpreter-side twin of the shell assignment
            # resolution.
            *_named(
                "test_interpreter_variable_bindings_are_inlined",
                ('n="{n}"; v="{v}"; subprocess.run([n,v])', _RULE_MINT),
                ('c="PKILL"; t="{n}"; subprocess.run([c,"-f",t])', _RULE_KILL),
            ),
            # Printf-style formatting is the same evasion as adjacent literal concatenation,
            # one operator along: by the time the sink runs it, it is one string.  The tuple
            # spelling is covered by consuming the arguments in order.
            *_named(
                "test_percent_format_join_inside_a_sink",
                ('os.system("{n} %s" % "{v}")', _RULE_MINT),
                ('os.system("%s %s" % ("{n}", "{v}"))', _RULE_MINT),
                ('os.system("PKILL -f %s" % "{n}")', _RULE_KILL),
            ),
        ],
    )
    def test_interpreter_assembled_payload_is_inlined(self, origin, payload, rule):
        body = payload.format(n=_NAME, v=_TOK).replace("PKILL", _PK)
        text = "python -c " + chr(39) + body + chr(39)
        expected = rule + ("-interpreter" if rule == _RULE_KILL else "-argv")
        assert _denied_by(text) == expected

    @pytest.mark.parametrize(
        "cmd,rule",
        [
            ("PKILL -f '{b}'", _RULE_KILL),
            ("PKILL -f {b}", _RULE_KILL),
            ("killall '{b}'", _RULE_KILL),
            ("kill $(PGREP -f '{b}')", _RULE_KILL),
        ],
    )
    def test_bracket_idiom_names_the_protected_program(self, cmd, rule):
        # `[k]irocrew` is the standard idiom for matching a process without matching the
        # grep itself.  A one-character bracket class expands to that character, so it
        # names the protected program; the class is collapsed before comparison.
        bracketed = "[" + _NAME[0] + "]" + _NAME[1:]
        text = cmd.format(b=bracketed).replace("PKILL", _PK).replace("PGREP", "p" + "grep")
        assert _denied_by(text) == rule

    def test_bracket_idiom_in_the_mint_program(self):
        spelled = _NAME[:4] + "[" + _NAME[4] + "]" + _NAME[5:]
        assert _denied_by(f"{spelled} {_TOK}") is not None

    def test_array_expanded_as_an_argument_is_data(self):
        # As an ARGUMENT the elements are just words: `echo ${a[@]}` prints them.  Only an
        # expansion in COMMAND position runs them.
        assert _denied_by(f"a=({_NAME} {_TOK}); echo ${{a[@]}}") is None

    def test_array_first_element_spelling_is_not_an_expansion(self):
        # bash reads `$a[@]` as `$a` followed by a literal `[@]` -- the first element
        # only, so the pair never runs and blocking it would be a false positive.
        assert _denied_by(f"a=({_NAME} {_TOK}); $a[@]") is None

    def test_percent_format_without_a_sink_allowed(self):
        # `print` does not execute, so collapsing the format must not make it a mint.
        text = "python3 -c " + chr(39) + 'print("' + _NAME + ' %s" % "' + _TOK + '")' + chr(39)
        assert _denied_by(text) is None

    def test_percent_format_with_a_non_literal_argument_allowed(self):
        # Only LITERAL arguments are substituted; a numeric format is left alone.
        text = "python3 -c " + chr(39) + 'x = "count: %d" % 5' + chr(39)
        assert _denied_by(text) is None

    def test_array_of_something_harmless_allowed(self):
        assert _denied_by('a=(ls -la); "${a[@]}"') is None

    def test_an_ordinary_glob_is_not_the_idiom(self):
        # Collapsing the class must not turn a normal glob into a match.
        assert _denied_by("ls [a]*.py") is None

    def test_two_quoted_halves_in_separate_statements_allowed(self):
        """Why the separator class was NOT widened to admit ``;``.

        Letting the quoted name and the quoted verb sit in DIFFERENT statements would
        match this, which mints nothing.  Inlining bindings instead keeps the argv
        pattern tight, and sink qualification still decides.
        """
        text = (
            "python3 -c "
            + chr(34)
            + "print("
            + chr(39)
            + _NAME
            + chr(39)
            + "); log("
            + chr(39)
            + _TOK
            + chr(39)
            + ")"
            + chr(34)
        )
        assert _denied_by(text) is None

    def test_binding_used_by_a_non_sink_allowed(self):
        text = "python3 -c " + chr(34) + "n=" + chr(39) + _NAME + chr(39) + "; print(n)" + chr(34)
        assert _denied_by(text) is None

    def test_sink_named_in_prose_allowed(self):
        # Naming a sink is not calling one; sink qualification still governs.
        assert _denied_by(f'git commit -m "wrap getoutput for {_NAME} {_TOK}"') is None

    def test_transformation_of_something_harmless_allowed(self):
        assert _denied_by("K=ls; ${K:0} /tmp") is None

    def test_transformation_naming_a_data_consumer_allowed(self):
        # Resolving the transformation must not lose the data-consumer exemption.
        assert _denied_by(f"K=echo; ${{K:0}} {_NAME} {_TOK}") is None

    def test_default_form_keeps_its_own_meaning(self):
        # `${x:-crew}` carries its own LITERAL and is resolved separately; the
        # transformation handling must not shadow it.
        assert _denied_by(f"kiro${{x:-crew}} {_TOK}") == _RULE_MINT

    def test_computed_value_that_is_not_the_verb_allowed(self):
        assert _denied_by("T=$(printf hello); echo $T") is None

    def test_indirect_expansion_of_something_harmless_allowed(self):
        assert _denied_by("A=ls; B=A; ${!B} /tmp") is None

    def test_indirect_expansion_with_no_binding_allowed(self):
        # Nothing is bound to `N`, so there is no literal to resolve to.
        assert _denied_by("V=N; echo ${!V}") is None

    @pytest.mark.parametrize(
        "prog,payload,rule",
        [
            ("python -c", "import os; os.system('p'+'kill -f {n}')", _RULE_KILL),
            ("python -c", "os.system('{n} '+'{v}')", _RULE_MINT),
            ("node -e", "execSync('p' + 'kill -f {n}')", _RULE_KILL),
        ],
    )
    def test_concatenated_literals_inside_a_sink(self, prog, payload, rule):
        # An interpreter joins adjacent string literals, so the sink receives ONE
        # command.  The two interpreter rules are also matched against a copy with
        # the joins collapsed -- scoped to those rules, not to every catalog rule.
        body = payload.format(n=_NAME, v=_TOK)
        text = prog + " " + Q + body + Q
        expected = rule + ("-interpreter" if rule == _RULE_KILL else "-argv")
        assert _denied_by(text) == expected

    def test_concatenation_without_a_sink_allowed(self):
        # `print` does not execute, so joining the literals must not make it a mint --
        # sink qualification still governs.
        assert _denied_by("python3 -c \"print('" + _NAME + " '+'" + _TOK + "')\"") is None

    def test_greedy_variable_name_is_not_a_concatenation(self):
        # bash parses `$xkill` as the variable `xkill` (unset), NOT `$x` followed by
        # "kill", so nothing runs -- blocking it would be a false positive.
        assert _denied_by(f"x=p; x=$xkill; $x -f {_NAME}") is None

    def test_reassignment_to_something_harmless_allowed(self):
        assert _denied_by("x=ls; x=${x} -la; $x /tmp") is None

    def test_function_body_is_attributed_to_its_own_program(self):
        # A function-body opener is a command boundary, so the body's program is `echo`
        # -- a data consumer -- and the words stay inert.
        body = "x()" + chr(123) + f" echo {_NAME} {_TOK};" + chr(125) + "; x"
        assert _denied_by(body) is None

    def test_printf_escape_without_an_evaluator_allowed(self):
        # Printing the words is not running them -- no evaluator, no payload.
        assert _denied_by(f"printf '{_NAME}\\040{_TOK}' > /tmp/notes.txt") is None

    def test_asyncio_name_without_a_sink_call_allowed(self):
        # Naming the function in prose is not calling it; sink qualification still governs.
        assert (
            _denied_by(f'git commit -m "wrap create_subprocess_shell for {_NAME} {_TOK}"') is None
        )

    def test_redirect_char_inside_a_quoted_target_is_still_a_kill(self, cmd=None):
        # `pkill -f` takes an ERE, so a `>` inside a quoted target belongs to the
        # PATTERN.  Normalizing it away as a redirect would discard the name, so the
        # target is searched raw as well as normalized.
        assert _denied_by(f"{_PK} -f '>{_NAME}'") == _RULE_KILL

    def test_nested_command_substitution_is_still_a_kill(self):
        # The PID-resolving substitution may contain one of its own, closing an
        # inner paren before the name appears; the gap must not stop there.
        cmd = f"{_K} $(pgrep -f \"$(printf '')" + _NAME + ' gateway")'
        assert _denied_by(cmd) == _RULE_KILL

    # --- incidental mentions: allowed ---

    def test_separator_confines_each_arm_to_one_segment(self):
        # A by-name kill in a LATER segment is still caught — segment scoping
        # narrows what one match may span, it does not skip segments.
        assert _denied_by(f"ls /tmp && {_PK} -f {_NAME}") == _RULE_KILL


class TestSelfKillArgvWindowIsQuoteAware:
    """The bare-``kill`` argv window survives a QUOTED close-paren decoy.

    ``_substitution_depth_delta`` counts parens on tokens the tokenizer already
    stripped the quotes from, so on its own it scores the quoted paren in
    ``kill $(printf ')' ; pgrep -f <name>)`` as a real closer, ends the window at
    the ``;``, and drops the ``pgrep`` clause that names the target -- while
    bash, whose substitution scan is quote-aware, runs that ``pgrep`` (measured).
    The window's substitution bodies are therefore also derived from the RAW text
    through the same quote-aware span scan the extractor uses, as a UNION with the
    token walk, so no spelling either walk detects is dropped.
    """

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            *_named(
                "test_quoted_paren_decoy_is_still_a_self_kill",
                # the decoy class: a quoted ')' inside the body
                "kill $(printf ')' ; pgrep -f {n})",
                # same decoy through the backtick spelling of the substitution
                "kill `printf ')' ; pgrep -f {n}`",
                # the kill matched at any argv position, as the token walk does
                "sudo kill $(printf ')' ; pgrep -f {n})",
                # the decoy inside a nested shell payload is the same command
                "sh -c 'kill $(printf \")\" ; pgrep -f {n})'",
                # a quoted separator is DATA: bash hands kill the substitution too
                "kill 123 ';' $(pgrep -f {n})",
                # ``&>`` is a redirect of the SAME command, not a separator: bash
                # runs ``kill <substitution output>`` (measured)
                "kill &>/dev/null $(printf ')' ; pgrep -f {n})",
                # ``>|`` (noclobber override) is the last separator-charactered
                # member of the redirect grammar -- same rule, measured
                "kill >|/dev/null $(printf ')' ; pgrep -f {n})",
                # an escaped backtick is DATA inside a backtick body, so it must
                # not be taken as the closer (measured)
                "kill `printf '\\`' ; pgrep -f {n}`",
                # a proven substitution INSIDE double quotes must not swallow the
                # rest of the line: the ``;`` after it is a real separator and the
                # kill segment after it is still scanned (measured)
                "echo \"$(date)\" ; kill $(printf ')' ; pgrep -f {n})",
                # the decoy fully inside double quotes: bash parses the body in a
                # fresh quote context, so the interior ')' stays data
                "kill \"$(printf ')' ; pgrep -f {n})\"",
                # quote-SPLICED kill: bash passes the word ``kill``, so the spliced
                # spelling with the decoy must not slip both union halves
                # (measured)
                "k''ill $(printf ')' ; pgrep -f {n})",
                "k'i'll $(printf ')' ; pgrep -f {n})",
                "\"ki\"ll $(printf ')' ; pgrep -f {n})",
                # an EMPTY substitution expands to nothing, so ``kill$()`` is the
                # word ``kill`` -- the glue exclusion must not eat the anchor
                # (measured)
                "kill$() $(printf ')' ; pgrep -f {n})",
                "kill$( ) $(pgrep -f {n})",
                # a NON-empty body can still expand to nothing at runtime
                # (``$(:)``, ``$(true)``), which no static scan decides -- a FIRST
                # word whose pre-glue prefix is ``kill`` keeps its anchor
                # (measured)
                "kill$(:) $(pgrep -f {n})",
                "kill$(:) $(printf ')' ; pgrep -f {n})",
                "kill$(true) `pgrep -f {n}`",
                # a variable an EARLIER command assigned the verb to reaches the
                # raw walk spelled ``$k`` while the token walk sees it resolved --
                # so the decoyed alias slipped both union halves (measured)
                "k=kill; $k $(printf ')' ; pgrep -f {n})",
                "k=kill; ${{k}} $(printf ')' ; pgrep -f {n})",
                "x=/usr/bin/kill; $x $(printf ')' ; pgrep -f {n})",
                # a command-position substitution whose OUTPUT is the verb: the
                # undecoyed spelling is already token-detected, so only the
                # decoyed combination needed the raw anchor (measured)
                "`printf kill` $(printf ')' ; pgrep -f {n})",
                "$(echo kill) $(printf ')' ; pgrep -f {n})",
            ),
            *_named(
                "test_control_spellings_remain_detected",
                # the undecorated control
                "kill $(pgrep -f {n})",
                # an unbalanced span is UNPROVEN: fail closed, scan the remainder
                "kill $(pgrep -f {n}",
                # decoyed AND unbalanced: only the raw pass sees this one, so it is
                # what discriminates its fail-closed remainder from an empty body
                "kill $(printf ')' ; pgrep -f {n}",
            ),
        ],
    )
    def test_quote_aware_window_still_sees_the_kill(self, origin, cmd):
        assert _denied_by(cmd.format(n=_NAME)) == _RULE_KILL

    @pytest.mark.parametrize(
        "cmd",
        [
            # intentional non-detections: the name is an operand of a DIFFERENT
            # command, so the raw-text window must not widen into denying them
            "kill 8123 && cp /tmp/{n}.json ~/",
            "kill 123; echo $(cat /tmp/{n})",
            # a comment is prose, not a command: bash never runs the pgrep
            "kill 123 # $(pgrep -f {n})",
            # single quotes make the whole thing data for echo
            "echo 'kill $(pgrep -f {n})'",
            # a substitution BEFORE the kill word is an environment word's
            # value, not the kill's operand -- the exact false positive the
            # token walk's own scoping replaced
            "LOG=$(ls /tmp/{n}.log) kill 4242",
            "nice -n $(cat /opt/{n}/etc/nice) kill -TERM 4242",
            # ``kill...`` glued to a substitution is an ARGUMENT of echo, not
            # a program: bash runs only the echo
            "echo kill$(printf {n})",
            # same glue, with a LATER substitution naming the product: the
            # glued word must not anchor the forward window either
            "echo kill$(printf x) $(pgrep -f {n})",
            # a proven double-quoted substitution must not absorb the rest of
            # the line into the kill's window
            'kill "$(printf 123)"; echo {n}',
            'kill "$(printf 123)" && cp /tmp/{n}.json ~/',
        ],
    )
    def test_scoped_non_detections_stay_allowed(self, cmd):
        assert _denied_by(cmd.format(n=_NAME)) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            # a REAL pipe or or-list still separates -- only the redirect
            # spellings (``2>&1``, ``&>``, ``>|``) ride inside a word.
            # Predicate-level: the raw regex TIER matches these across the
            # pipe (its ``[^;&#>]`` class admits ``|``), so the public-gate
            # verdict is owned by that tier, not this walk.
            "kill 123 | grep $(cat /tmp/{n})",
            "kill 123 || echo $(cat /tmp/{n})",
        ],
    )
    def test_pipe_still_separates_the_argv_window(self, cmd):
        from kiro_crew import security

        assert security._is_self_kill(cmd.format(n=_NAME).lower()) is False


class TestSelfKillRawWindowSearchesDequotedView:
    """The raw-window bodies are ALSO searched in a de-quoted, tokenized view.

    Two windows scan a bare ``kill``'s substitution bodies, and each has a blind
    spot the other covers -- until one spelling lands in the intersection.  The
    TOKEN window bounds the argv with ``_substitution_depth_delta``, a character
    counter that scores a ``case`` PATTERN's ``)`` (the token ``x)``) as a
    substitution closer, so a lookup placed after ``case ... esac`` in the body's
    command list falls outside its window.  The RAW window is immune to that --
    it extracts the body whole through the quote-aware span scan -- but raw text
    and ``_resolve_param_defaults`` remove no quotes, so an adjacent-quote
    concatenation of the product name (``'kiro''crew'``) does not read as the
    name there.  Either miss alone is survivable (the unquoted ``esac``-tail
    spelling is denied by the raw window; the quote-spliced name in a plain list
    is denied by the token window); the combination would return ``None`` while
    bash runs the lookup (measured).

    So each raw body's words are also searched -- the ``_self_tokens`` view, which
    folds a backslash-newline split whole and swallows an untokenizable body
    instead of raising out of the gate -- each word BARE and through
    ``_resolved_word_view`` (parameter defaults resolved, empty substitutions
    collapsed, bracket classes removed), so quote removal composes with the
    transforms the ``pkill`` leg gets from ``_normalize_operand``.  Single
    transforms are not enough: a name needing two of them at once
    (``'kiro''[c]rew'``, ``'kiro'${x:-crew}``, ``kiro$()crew``) sits in the
    seam between any pair of single-transform searches.  Both members carry
    weight in opposite directions: the composed transform reveals a name
    de-quoting alone leaves hidden, and the bare search keeps a name the
    transform DESTROYS -- a pattern-substitution expansion
    (``${PATH/usr/|'kiro''crew'|zz-}``) de-quotes to a word carrying the name,
    then resolves to an empty default, and bash runs it.  The transform is
    deliberately NOT ``_normalize_operand``: a de-quoted pgrep pattern is an
    ERE whose own characters (``'zz|kiro'crew``, ``'>kiro'crew``) an operand
    view truncates at, discarding exactly the protected alternative.
    Monotone in the deny direction: the de-quoted view only adds searches; it
    narrows or replaces none.
    """

    # Two adjacent quoted fragments -- the measured spelling.
    _SPLICED = "'" + _NAME[:4] + "''" + _NAME[4:] + "'"
    # The ANSI-C spelling of the same concatenation.
    _ANSI = "$'" + _NAME[:4] + "'$'" + _NAME[4:] + "'"

    @pytest.mark.parametrize(
        "cmd",
        [
            # lookup AFTER ``case ... esac``, spliced name
            "kill $(case x in x) :;; esac; pgrep -f {q})",
            # the same tail behind the other list operators
            "kill $(case x in x) :;; esac && pgrep -f {q})",
            "kill $(case x in x) :;; esac | pgrep -f {q})",
            # a nested compound: the clause buried one level deeper
            "kill $(if true; then case x in x) :;; esac; fi; pgrep -f {q})",
            # the backtick spelling of the same substitution
            "kill `case x in x) :;; esac; pgrep -f {q}`",
            # the ANSI-C spelling of the name concatenation
            "kill $(case x in x) :;; esac; pgrep -f {a})",
            # COMPOSED transforms: a name needing quote removal AND parameter-
            # default resolution, in tail and head position -- each transform
            # alone leaves the name invisible, so per-word operand
            # normalization is what reaches these
            "kill $(case x in x) :;; esac; pgrep -f 'kiro'${{x:-crew}})",
            "kill $(case x in x) :;; esac; pgrep -f ${{x:-kiro}}'crew')",
            "kill $(case x in x) :;; esac; pgrep -f $'kiro'${{x:-crew}})",
            # a statically EMPTY substitution splices the name (both spellings)
            "kill $(case x in x) :;; esac; pgrep -f kiro$()crew)",
            "kill $(case x in x) :;; esac; pgrep -f kiro``crew)",
            # the [c] bracket-class trick composed with the quote splice --
            # the de-quoted word still needs the bracket removal its two
            # sibling searches already apply
            "kill $(case x in x) :;; esac; pgrep -f 'kiro''[c]rew')",
            "kill $(case x in x) :;; esac; pgrep -f '[k]iro''crew')",
            # all three transforms at once: bracket class + empty substitution
            # + parameter default
            "kill $(case x in x) :;; esac; pgrep -f '[k]iro'$()${{x:-crew}})",
            # a line continuation splits the name across raw lines; only the
            # continuation-folded token view reads it whole
            "kill $(case x in x) :;; esac; pgrep -f kiro\\\ncrew)",
            # a quoted ERE alternation prefix: the pattern bash passes is
            # zz|kirocrew, whose second alternative matches protected
            # processes -- an operator-truncating transform discards exactly
            # the protected half, so the per-word transform must resolve
            # defaults, empty substitutions and bracket classes WITHOUT
            # treating the de-quoted pattern's own characters as boundaries
            "kill $(case x in x) :;; esac; pgrep -f 'zz|kiro'${{x:-crew}})",
            "kill $(case x in x) :;; esac; pgrep -f 'zz|kiro'$()crew)",
            "kill $(case x in x) :;; esac; pgrep -f 'zz|[k]iro'$()${{x:-crew}})",
            # a redirect-prefixed ERE (the pkill leg's own documented case: a
            # ``>`` inside a pattern is part of the TARGET) -- pins that the
            # word search never routes through an operator-truncating view
            "kill $(case x in x) :;; esac; pgrep -f '>kiro''crew')",
            # a pattern-substitution expansion DESTROYS the de-quote-visible
            # name (the ${{...}} name class swallows it and the resolved
            # default is the empty tail), so the untransformed word view is
            # what reaches it -- bash expands ${{PATH/usr/|<name>|zz-}} to an
            # ERE carrying the name as its own alternative and runs the lookup
            "kill $(case x in x) :;; esac; pgrep -f ${{PATH/usr/|{q}|zz-}})",
            "kill `case x in x) :;; esac; pgrep -f ${{PATH/usr/|{q}|zz-}}`",
        ],
    )
    def test_dequoted_search_reaches_the_esac_tail(self, cmd):
        assert _denied_by(cmd.format(q=self._SPLICED, a=self._ANSI)) == _RULE_KILL

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            *_named(
                "test_sibling_spellings_stay_denied",
                # the load-bearing control: SAME esac-tail spelling, name unquoted --
                # denied by the raw window's raw-text search alone (the
                # case-pattern miss alone is survivable)
                "kill $(case x in x) :;; esac; pgrep -f {n})",
                # spliced name INSIDE the case clause: token window reaches it
                "kill $(case x in x) pgrep -f {q};; esac)",
                # spliced name in a plain list: token window reaches it
                "kill $(:; pgrep -f {q})",
                # ``;;`` inside quotes is data, not a clause terminator
                "kill $(echo ';;'; pgrep -f {q})",
            ),
            *_named(
                "test_a_redirected_mention_in_a_kill_owned_body_stays_denied",
                # A redirected mention INSIDE a kill-owned body is denied even
                # though only the second printf's output reaches the outer kill.
                # Deliberate fail-closed over-approximation, NOT a new class: no
                # search in this module performs output-flow analysis on a body --
                # the same command with the name unquoted is denied by the raw
                # search, and the plain-list spelling of this very command is
                # denied by the token window -- so the de-quoted leg matching here
                # only removes quote-sensitivity from the existing posture.
                "kill $(case x in x) :;; esac; printf {q} >/dev/null; printf 4242)",
                "kill $(printf {q} >/dev/null; printf 4242)",
                "kill $(case x in x) :;; esac; printf {n} >/dev/null; printf 4242)",
            ),
        ],
    )
    def test_dequoted_view_keeps_these_denied(self, origin, cmd):
        assert _denied_by(cmd.format(n=_NAME, q=self._SPLICED)) == _RULE_KILL

    @pytest.mark.parametrize(
        "cmd",
        [
            # QUOTED-LITERAL construct text: bash passes these patterns with
            # the $()/backticks as literal ERE characters, so they cannot
            # select a normally-named process -- but the module has never
            # tracked quote provenance into a pattern operand: the pkill leg
            # and the token window already deny every plain-list and pkill
            # twin of these on the same collapsed view (measured), so the
            # composed leg matching them only removes window-sensitivity from
            # the existing fail-closed posture.
            "kill $(case x in x) :;; esac; pgrep -f 'kiro$()crew')",
            "kill $(case x in x) :;; esac; pgrep -f 'kiro``crew')",
            "kill $(case x in x) :;; esac; pgrep -f kiro'$()'crew)",
            "kill $(case x in x) :;; esac; pgrep -f kiro\\$\\(\\)crew)",
        ],
    )
    def test_a_quoted_literal_construct_pattern_stays_denied(self, cmd):
        assert _denied_by(cmd) == _RULE_KILL

    def test_a_nul_poisoned_body_returns_a_verdict_instead_of_raising(self):
        # A literal NUL after a word-initial ``~`` inside the body made the
        # tokenizing view raise (``expanduser``: embedded null byte) and the
        # exception escaped through ``is_denied`` -- the gate returned neither
        # allow nor deny.  The floor's contract for an untokenizable body is a
        # swallowed view, never an escaping exception, so the verdict itself
        # is not pinned here -- only that one is returned.
        cmd = "kill `~\x00; pgrep -f " + self._SPLICED + "`"
        assert _denied_by(cmd) in (None, _RULE_KILL)

    @pytest.mark.parametrize(
        "cmd",
        [
            # the module's own named allowances: a PID kill next to a command
            # that merely mentions a product path ...
            "kill 8123 && cp /tmp/{n}.json ~/",
            # ... a substitution PRECEDING the kill (an environment word) ...
            "LOG=$(ls /tmp/{n}.log) kill 4242",
            # ... and a substitution belonging to a DIFFERENT command
            "kill 123; echo $(cat /tmp/{n})",
            # the esac-tail body with NO kill anywhere: not this rule's concern
            "echo $(case x in x) :;; esac; pgrep -f {q})",
        ],
    )
    def test_named_allowances_stay_allowed(self, cmd):
        assert _denied_by(cmd.format(n=_NAME, q=self._SPLICED)) is None
