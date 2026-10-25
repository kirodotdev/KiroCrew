"""Tests for the credential-mint floor.

An interpreter payload that spawns the CLI through a library call is a mint, and the mint
rule matches the name and the verb within one command segment.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import (
    _HYPH,
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


class TestInterpreterArgvLiteralMint:
    """An interpreter payload that spawns the CLI through a library call.

    ``python -c "subprocess.run(['<name>','<verb>'])"`` mints the credential without
    ever writing the command as a shell word. The argv floor cannot help: the payload
    is one opaque token to the shell tokenizer and its contents are Python, not shell.

    Scoped to the two words as ADJACENT QUOTED ARGUMENTS -- the shape every such argv
    literal takes. The separator class admits only what appears BETWEEN argv elements
    (quote, comma, whitespace, opening bracket/paren), which is what keeps a regex
    LITERAL quoting this very rule, and prose naming both words, from matching.
    """

    _RULE = _RULE_MINT + "-argv"

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            *_named(
                "test_argv_literal_blocked",
                f"python -c \"import subprocess; subprocess.run(['{_NAME}','{_TOK}'])\"",
                f"python3 -c \"import subprocess; subprocess.run(['{_NAME}', '{_TOK}'])\"",
                f'node -e \'require("child_process").execFileSync("{_NAME}",["{_TOK}"])\'',
                f"python -c \"import os; os.execvp('{_NAME}', ['{_NAME}', '{_TOK}'])\"",
                f'perl -e \'system("{_NAME}","{_TOK}")\'',
                f'ruby -e \'system "{_NAME}", "{_TOK}"\'',
            ),
            # An argv literal may carry global options between the program and the verb, so
            # the separator class admits the characters a quoted FLAG is made of.  It stays
            # ONE flat character class rather than a repeated group: a group carrying its own
            # quantifier is rejected by `_redos_prone`, and a rejected pattern is a DISABLED
            # pattern -- the rule would sit in the catalog matching nothing.
            *_named(
                "test_intervening_quoted_flags_still_blocked",
                f"python -c \"subprocess.run(['{_NAME}','--no-jail','{_TOK}'])\"",
                f"python -c \"subprocess.run(['{_NAME}', '-v', '--no-jail', '{_TOK}'])\"",
                f'node -e \'execFileSync("{_NAME}",["--json","{_TOK}"])\'',
            ),
        ],
    )
    def test_interpreter_argv_literal_blocked(self, origin, cmd):
        assert _denied_by(cmd) == self._RULE

    @pytest.mark.parametrize(
        "cmd",
        [
            # the recorded false positive this PR exists to remove -- separated by
            # `.*`, which the separator class excludes
            f"python3 -c \"import re; re.search(r'.*{_NAME}.*{_TOK}', cmd)\"",
            f"python3 -c \"print('{_NAME}')\"; echo {_TOK}",
            f"jq -r '.{_NAME} , .{_TOK}' cfg.json",
            f"node -e 'console.log(\"{_NAME} docs mention {_TOK}\")'",
            f"git commit -m 'note: {_NAME} {_TOK} rule'",
        ],
    )
    def test_mentions_and_regex_literals_allowed(self, cmd):
        assert _denied_by(cmd) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            "python -c 'os.system(\"{n} {v}\")'",
            "python -c 'os.popen(\"{n} {v}\")'",
            'node -e \'require("child_process").execSync("{n} {v}")\'',
            "php -r 'shell_exec(\"{n} {v}\");'",
            "ruby -e 'system(\"{n} {v}\")'",
        ],
    )
    def test_sink_qualified_single_string_blocked(self, cmd):
        """The single-string form, closed by qualifying it on an EXECUTING sink.

        Two words inside one quoted string is textually identical to prose, so the
        broad co-occurrence rule this PR removes cannot be the answer. Requiring an
        execution sink in front of the string separates them: `os.system(...)` /
        `execSync(...)` run it, while `re.search(...)`, a commit message and
        `console.log(...)` do not and stay allowed.
        """
        assert _denied_by(cmd.format(n=_NAME, v=_TOK)) is not None

    @pytest.mark.parametrize(
        "cmd",
        [
            "python -c 'os.system(\"PKILL -f {n}\")'",
            'node -e \'require("child_process").execSync("PKILL -f {n}")\'',
            "php -r 'shell_exec(\"KILLALL {n}\");'",
        ],
    )
    def test_sink_qualified_single_string_kill_blocked(self, cmd):
        text = cmd.format(n=_NAME).replace("PKILL", _PK).replace("KILLALL", _KA)
        assert _denied_by(text) is not None

    @pytest.mark.parametrize(
        "cmd",
        [
            "python -c \"subprocess.run(['{n}'] + ['{v}'])\"",
            "python -c \"subprocess.run(['PKILL','-f','{n}'])\"",
            "python -c \"subprocess.run(['KILLALL','{n}'])\"",
            'node -e \'spawnSync("PKILL",["-f","{n}"])\'',
        ],
    )
    def test_argv_list_and_concatenation_blocked(self, cmd):
        # An argv literal can be assembled by list concatenation, and the kill verb takes
        # the same argv-list shape the mint does.  Both alternatives cover it.
        text = cmd.format(n=_NAME, v=_TOK).replace("PKILL", _PK).replace("KILLALL", _KA)
        assert _denied_by(text) is not None

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            # A `;` INSIDE `$( ... )` belongs to that substitution, not to the argv being
            # scanned -- `<name> $(true; echo <verb>)` is one command.  The scan tracks
            # substitution depth so only a top-level separator ends it.
            *_named(
                "test_separator_nested_in_a_substitution_does_not_end_the_argv",
                "{n} $(true; echo {v})",
                "{n} $(echo {v})",
                "PKILL -f $(true; echo {n})",
            ),
            # `env -S` splits its argument into a command and execs it, so the payload is a
            # command line like a `-c` argument.  The flag arrives lowercased (`is_denied`
            # lowercases its input), which is why the comparison is case-insensitive.
            *_named(
                "test_env_split_string_payload_blocked",
                "env -S '{n} {v}'",
                "env -S'{n} {v}'",
                "env --split-string '{n} {v}'",
                "env --split-string='{n} {v}'",
                "env -S 'PKILL -f {n}'",
            ),
            # `xargs` does not read a script -- it APPENDS the piped words to its own
            # command, so `echo <verb> | xargs <name>` runs `<name> <verb>` even though
            # neither half contains a space.  The effective command line is reconstructed so
            # the ordinary argv checks can see it.
            *_named(
                "test_xargs_appended_arguments_blocked",
                "echo {v} | xargs {n}",
                "echo {v} | xargs -n1 {n}",
                "echo {n} | xargs PKILL -f",
            ),
            # `f"..."`, `rb"..."` and friends put a prefix between the sink's paren and the
            # opening quote, which the opener did not admit.
            *_named(
                "test_string_prefix_before_the_payload_blocked",
                "python -c 'os.system(f\"{n} {v}\")'",
                "python -c 'os.system(f\"PKILL -f {n}\")'",
                "python -c 'os.system(rb\"{n} {v}\")'",
            ),
            # An interpreter argv can name the program by ABSOLUTE or relative path.  The
            # quoted program-name patterns admit a path prefix, written as one flat
            # character class rather than a repeated group -- a group carrying its own
            # quantifier is rejected by `_redos_prone`, and a rejected pattern is DISABLED.
            *_named(
                "test_path_qualified_program_in_interpreter_argv_blocked",
                "python -c \"subprocess.run(['/usr/bin/{n}','{v}'])\"",
                "python -c \"subprocess.run(['./bin/{n}','{v}'])\"",
                "python -c \"subprocess.run(['/usr/bin/PKILL','-f','{n}'])\"",
                'node -e \'execFileSync("/opt/{n}",["{v}"])\'',
                "python -c 'os.system(\"/usr/bin/{n} {v}\")'",
            ),
            # `printf` expands `\n` before the shell sees it, so the payload is decoded the
            # same way before re-tokenizing.  Without that, the escape glues onto the verb
            # and the comparison misses.
            *_named(
                "test_printf_escaped_payload_blocked",
                "printf '{n} {v}\\n' | bash",
                "printf '{n} {v}\\n' | sh",
                "printf 'PKILL -f {n}\\n' | bash",
                "echo -e '{n} {v}\\n' | bash",
            ),
            # `*['<verb>']` unpacks into the argv, so `*` joins the argv separator class.  It
            # stays OUT of the command-position gap on purpose -- that exclusion is what keeps
            # a regex literal (`.*<name>.*<verb>`) from matching, and here every element is
            # quoted so the literal still cannot.
            *_named(
                "test_star_unpacked_argv_blocked",
                "python -c \"subprocess.run(['{n}', *['{v}']])\"",
                "python -c \"subprocess.run(['PKILL', *['-f','{n}']])\"",
            ),
        ],
    )
    def test_interpreter_payload_spellings_still_blocked(self, origin, cmd):
        text = cmd.format(n=_NAME, v=_TOK).replace("PKILL", _PK)
        assert _denied_by(text) is not None

    @pytest.mark.parametrize(
        "cmd",
        [
            "python -c 'os.kill(pid_from(\"[k]irocrew gateway\"), 9)'",
            "python -c 'os.killpg(pgid_of(\"{n}\"), 15)'",
            "node -e 'process.kill(pidOf(\"{n}\"), 9)'",
        ],
    )
    def test_direct_kill_api_blocked(self, cmd):
        # `os.kill` IS the execution sink, so it stands as its own alternative rather than
        # behind the shell-command sink list.  Matched on `irocrew` rather than the full
        # name so the standard "don't match my own lookup" bracket idiom (`[k]irocrew`),
        # which still resolves to the gateway, is not a free pass.
        assert _denied_by(cmd.format(n=_NAME)) is not None

    def test_long_gap_inside_the_quoted_string_still_blocked(self):
        # The gap between name and verb inside one quoted string is unbounded: a
        # fixed `{0,80}` bound is escapable with 81 spaces.
        cmd = "python -c 'os.system(\"" + _NAME + " " * 90 + _TOK + "\")'"
        assert _denied_by(cmd) is not None

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            *_named(
                "test_xargs_without_a_protected_command_allowed",
                "echo hi | xargs ls",
                "echo /workplace/alice/{n}-wt-x | xargs ls",
            ),
            # The substitution belongs to a DIFFERENT command on the line.  Scanning every
            # substitution in the whole text would associate them all with any `kill`
            # present and deny this, so the scan is confined to the kill's own argv.
            *_named(
                "test_substitution_outside_the_kill_argv_allowed",
                "kill 123; echo $(cat /tmp/{n})",
                "kill 123; ls $(dirname /tmp/{n}/x)",
            ),
        ],
    )
    def test_payload_without_a_protected_command_allowed(self, origin, cmd):
        assert _denied_by(cmd.format(n=_NAME)) is None

    @pytest.mark.parametrize(
        ("origin", "cmd", "rule"),
        [
            # `X=<name>;$X <verb>` glues the assignment and the command that uses it into ONE
            # token, so neither was seen.  Tokens are split on top-level control operators
            # before assignments are resolved.
            *_named(
                "test_glued_assignment_separator_still_resolved",
                ("X={n};$X {v}", _RULE_MINT),
                ("X=PKILL;$X -f {n}", _RULE_KILL),
            ),
            # `x(){ <name> "$@";}; x <verb>` never puts the program and the verb in one argv:
            # the body holds the program, the call site holds the verb.  A function whose body
            # invokes a protected program is therefore treated as an alias for it, so the
            # ordinary argv checks see the real command at the call site.
            *_named(
                "test_function_forwarding_arguments_blocked",
                ('x(){{ {n} "$@";}}; x {v}', _RULE_MINT),
                ('x(){{ {n} "$1";}}; x {v}', _RULE_MINT),
                ('function x(){{ {n} "$@";}}; x {v}', _RULE_MINT),
                ('x(){{ PKILL -f "$1";}}; x {n}', _RULE_KILL),
                ("k(){{ PKILL -f {n};}}; k", _RULE_KILL),
            ),
            # `alias x=<name>` then `x <verb>` is the same evasion as a function wrapper --
            # the definition holds the program, the call site holds the verb -- so aliases
            # resolve alongside function definitions.
            *_named(
                "test_alias_definition_resolved",
                ("bash -c 'alias x={n}; x {v}'", _RULE_MINT),
                ("bash -c 'alias k=PKILL; k -f {n}'", _RULE_KILL),
            ),
            # `$(printf <name>) <verb>` puts the data consumer INSIDE a substitution that
            # occupies program position, so its OUTPUT is what runs.  The "arguments are just
            # data" exemption must not apply there.
            *_named(
                "test_substitution_in_program_position_is_not_inert_data",
                ("$(printf {n}) {v}", _RULE_MINT),
                ("`printf {n}` {v}", _RULE_MINT),
                ("$(echo {n}) {v}", _RULE_MINT),
                ("$(printf PKILL) -f {n}", _RULE_KILL),
            ),
            # bash runs the inner command of a PROCESS substitution (`<( )`, `>( )`) exactly as
            # it does for a command substitution, so every substitution body is walked as a
            # payload and the ordinary argv checks see the inner invocation.
            *_named(
                "test_process_substitution_body_is_a_command",
                ("cat <({n} {v})", _RULE_MINT),
                ("diff <({n} {v}) /tmp/x", _RULE_MINT),
                ("tee >({n} {v})", _RULE_MINT),
                ("cat <(PKILL -f {n})", _RULE_KILL),
            ),
            # A multiword alias replacement is a whole COMMAND LINE, not just a program name,
            # so it is handed to the payload walk rather than treated as an alias target.
            *_named(
                "test_multiword_alias_replacement_blocked",
                ("bash -c \"alias x='{n} {v}'; x\"", _RULE_MINT),
                ("bash -c \"alias k='PKILL -f {n}'; k\"", _RULE_KILL),
            ),
        ],
    )
    def test_resolved_payload_is_judged_by_its_rule(self, origin, cmd, rule):
        text = cmd.format(n=_NAME, v=_TOK).replace("PKILL", _PK)
        assert _denied_by(text) == rule

    def test_parameter_expansion_inside_the_verb_blocked(self):
        # `t${X-}oken` expands to the verb once X is unset, so the operand normalizer
        # resolves literal parameter-expansion defaults before comparing.
        assert _denied_by(f"unset X; {_NAME} t${{X-}}" + _TOK[1:]) is not None
        assert _denied_by(f"unset X; {_NAME} ${{X-{_TOK}}}") is not None

    @pytest.mark.parametrize(
        "cmd",
        [
            'x(){{ ls "$@";}}; x {v}',
            "x(){{ echo {n} {v};}}; x",
        ],
    )
    def test_function_not_forwarding_to_a_protected_program_allowed(self, cmd):
        # The alias only forms when the BODY invokes a protected program: a body that
        # merely prints the words, or invokes something else, is not an alias.
        assert _denied_by(cmd.format(n=_NAME, v=_TOK)) is None

    def test_parameter_expansion_inside_a_substitution_body_is_still_a_kill(self):
        # `kill $(pgrep -f kiro${x:-crew})` hides the name behind an expansion whose
        # literal branch the shell substitutes back in, so the body is searched both raw
        # and with those defaults resolved.
        assert _denied_by("kill $(pgrep -f kiro${x:-crew})") == _RULE_KILL

    def test_kill_api_mentioned_in_prose_allowed(self):
        assert _denied_by(f"echo 'os.kill docs mention {_NAME}'") is None

    @pytest.mark.parametrize(
        "cmd",
        [
            "python -c 'os.system(\"PKILL -f [k]irocrew\")'",
            "node -e 'execSync(\"PKILL -f [k]irocrew\")'",
            "python -c \"subprocess.run(['PKILL','-f','[k]irocrew'])\"",
        ],
    )
    def test_bracket_idiom_inside_an_interpreter_payload_blocked(self, cmd):
        # `[k]irocrew` is the standard "don't match my own process lookup" idiom and still
        # resolves to the gateway.  The direct-kill-API branch and the sink-qualified
        # branches all account for it, so the three are consistent.
        assert _denied_by(cmd.replace("PKILL", _PK)) is not None

    def test_process_substitution_of_something_harmless_allowed(self):
        assert _denied_by(f"cat <(ls /workplace/alice/{_NAME}-wt-x)") is None

    def test_multiword_alias_to_something_harmless_allowed(self):
        assert _denied_by("bash -c \"alias x='ls -la'; x\"") is None

    def test_bracket_idiom_in_prose_still_allowed(self):
        # Tolerating the idiom must not turn a mention into a match: no execution sink,
        # no denial.
        assert _denied_by(f"echo 'run {_PK} [k]irocrew to stop it'") is None
        assert _denied_by(f"git commit -m 'note: {_PK} [k]irocrew rule'") is None

    def test_data_consumer_not_in_program_position_still_allowed(self):
        # The exemption still holds for an ordinary consumer invocation.
        assert _denied_by(f"printf '%s' {_NAME} {_TOK}") is None
        assert _denied_by(f"echo {_NAME} {_TOK}") is None

    def test_alias_to_an_unprotected_program_allowed(self):
        assert _denied_by(f"bash -c 'alias x=ls; x /workplace/alice/{_NAME}-wt-x'") is None
        assert _denied_by("printf '%s\\n' hello | bash") is None

    def test_env_without_a_protected_payload_allowed(self):
        assert _denied_by(f"env -S 'ls /workplace/alice/{_NAME}-wt-x'") is None
        assert _denied_by(f"env FOO=1 ls /workplace/alice/{_NAME}-wt-x") is None

    @pytest.mark.parametrize(
        "cmd",
        [
            "node -e 'console.log(\"run {n} {v} to mint\")'",
            "echo 'run PKILL {n} to stop it'",
            "python3 -c \"print('{n} docs mention {v}')\"",
            "git commit -m 'note: PKILL {n} rule'",
        ],
    )
    def test_no_sink_means_no_match(self, cmd):
        # The sink is doing the work: the same two words with a NON-executing call, or
        # none at all, stay allowed.  This is what the broad rule could not do.
        text = cmd.format(n=_NAME, v=_TOK).replace("PKILL", _PK)
        assert _denied_by(text) is None

    def test_literal_concatenation_is_no_longer_the_gap(self):
        """Adjacent string LITERALS are joined before matching."""
        assembled = (
            "python -c 'import os; os.system("
            + Q
            + "kiro"
            + Q
            + " + "
            + Q
            + "crew "
            + _TOK
            + Q
            + ")'"
        )
        assert _denied_by(assembled) == _RULE_MINT + "-argv"

    def test_a_base64_wrapped_mint_is_decoded_and_denied(self):
        """An inline payload hiding the mint in a base64 literal is DENIED -- by decoding it.

        `base64.b64decode("a2lyb2NyZXcgdG9rZW4=")` is `kirocrew token`; the floor decodes
        every base64-shaped literal of the command AS SUBMITTED (base64 does not survive
        the lower-casing the rest of the floor reads) and matches the decoded text. This
        replaces the earlier reading, under which the `b64decode`/`exec`/`__import__`
        wrapper was itself the tell and the whole `-c` invocation was refused as opaque --
        a reading that denied every inline `getattr`/`eval`/`importlib` one-liner in the
        product tree (143 denials, zero mints, in eight days of the audit log) while a
        payload that wanted to hide could always use `chr()` arithmetic instead, as the
        next test records.
        """
        computed = (
            "python -c 'import os,base64; os.system(base64.b64decode("
            + Q
            + "a2lyb2NyZXcgdG9rZW4="
            + Q
            + ").decode())'"
        )
        assert _denied_by(computed) is not None
        # The same wrapper around a literal that decodes to something else is not a mint:
        # `aGVsbG8gd29ybGQ=` is `hello world`.
        benign = (
            "python -c 'import os,base64; print(base64.b64decode("
            + Q
            + "aGVsbG8gd29ybGQ="
            + Q
            + ").decode())'"
        )
        assert _denied_by(benign) is None

    def test_the_true_residual_gap_is_a_name_no_matcher_can_see(self):
        """What genuinely remains uncovered, and why the real guarantee is elsewhere.

        The dynamic-exec deny catches the COMMON primitives, but a determined payload can
        still avoid every one of them — build the string with `chr()` arithmetic, read it from
        a file, fetch it over a socket, or simply write a script and run it as a second
        command. None of those names anything a static command-text rule matches, and no such
        rule ever could: arbitrary code running as the same OS user is outside a string
        matcher's reach. That is precisely why the UN-DISABLEABLE guarantee for this credential
        is the sensitive-path floor over the signing key (`.local_secret` is read+write blocked
        on both the tool and shell gates), not these defense-in-depth deny rules.
        """
        # A name assembled by `chr()` arithmetic — no decode/exec/import verb, no literal.
        chr_built = (
            "python -c 'import os; os.system("
            "chr(107)+chr(105)+chr(114)+chr(111)+chr(99)+chr(114)+chr(101)+chr(119))'"
        )
        assert _denied_by(chr_built) is None
        # Write-then-run: the program text is in a file the deny rules never see.
        two_step = "printf 'x' > /tmp/s.py && python /tmp/s.py"
        assert _denied_by(two_step) is None


class TestCredentialMintSegmentScoping:
    """The credential-mint rule matches name + verb within one command segment.

    The retired pattern was two words co-occurring in order anywhere in the
    command, which blocked ordinary work under a product-named path and let
    the hyphenated CLI spelling through.

    Scoping is ARGV-STRUCTURAL rather than textual on purpose: the mint is
    "the CLI's own argv contains the verb".  Anchoring the verb to the name in
    raw text is defeated by ordinary shell forms that put something between them
    (a quoted verb, extra global flags, an empty parameter expansion, a
    redirection), and widening the gap to admit those also admits a filesystem
    path -- which is the false positive the rule exists to avoid.
    """

    # --- real mints: still blocked ---

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            *_named(
                "test_mint_invocation_still_blocked",
                f"{_NAME} {_TOK}",
                f"{_NAME} {_TOK} --ttl 1h",
                f"{_NAME} {_TOK} --port 6777",
                f"./bin/{_NAME} {_TOK}",
                f"ssh remote-host {_NAME} {_TOK}",
                f'"$HOME/bin/{_NAME}" {_TOK} --ttl 30m',
                f'"$HOME/bin/{_NAME}" {_TOK} --ttl 20h --port 7879',
                f'"/opt/custom/{_NAME}" {_TOK}',
                f"$(which {_NAME}) {_TOK}",
                f"docker exec {_NAME} {_NAME} {_TOK} --ttl 2h",
                f"KIROCREW_HOME=/tmp/h KIROCREW_PORT=6777 {_NAME} {_TOK}",
            ),
            # A mint reached through a subcommand word is still a mint; the retired
            # pattern covered it via its unbounded gap and this must not regress.
            *_named(
                "test_nested_subcommand_mint_still_blocked",
                f"{_NAME} pod {_TOK} mywt",
                f"{_NAME} pod {_TOK} mywt --ttl 2h",
            ),
            # The shell strips quotes, an empty expansion and an empty substitution
            # before the CLI ever runs, and global flags may precede the verb, so
            # each of these mints a credential exactly as the bare form does.
            *_named(
                "test_shell_forms_between_name_and_verb_still_blocked",
                f'{_NAME} "{_TOK}"',
                f"{_NAME} '{_TOK}'",
                f"{_NAME} -v --no-jail {_TOK}",
                f"unset __EMPTY; {_NAME} ${{__EMPTY:-}} {_TOK}",
                f"{_NAME} $(printf '') {_TOK}",
            ),
            # bash accepts a redirection ANYWHERE in a simple command, so
            # `<name> >/tmp/out <verb>` runs the mint and writes the signed URL to a
            # file.  A raw-string pattern cannot step over the redirect without also
            # stepping over a path, which is why enforcement is argv-structural.
            *_named(
                "test_redirection_between_name_and_verb_still_blocked",
                f"{_NAME} >/tmp/out {_TOK}",
                f"{_NAME} 2>/dev/null {_TOK}",
                f"{_NAME} >/tmp/o {_TOK} --ttl 1h",
                f"{_NAME} >>/tmp/o {_TOK}",
            ),
            # A shell's `-c` argument is a COMMAND, not an operand: tokenizing the
            # outer command leaves the mint as one opaque token, so the payload is
            # re-tokenized and its argv checked too.  The last case needs that
            # descent specifically -- the redirect form is invisible to the raw-text
            # half of the union.
            *_named(
                "test_nested_shell_payload_still_blocked",
                f'bash -c "{_NAME} {_TOK}"',
                f"bash -c '{_NAME} {_TOK}'",
                f'sh -c "{_NAME} {_TOK}"',
                f'/bin/bash -c "{_NAME} {_TOK}"',
                f'bash -lc "{_NAME} {_TOK}"',
                f'zsh -c "{_NAME} pod {_TOK} wt"',
                f'eval "{_NAME} {_TOK}"',
                f"bash -c 'bash -c \"{_NAME} {_TOK}\"'",
                f'bash -c "{_NAME} >/tmp/o {_TOK}"',
            ),
            # `-c` arrives inside a COMBINED short-flag cluster (`-xc`, `-ec`, `-icx`)
            # just as readily as alone, and the program name may itself come from a
            # substitution.  Matching only the exact spellings left every other
            # cluster as a bypass.
            *_named(
                "test_combined_shell_flag_payload_still_blocked",
                f"bash -xc '{_NAME} >/tmp/o {_TOK}'",
                f"bash -ec '{_NAME} {_TOK}'",
                f"sh -xc '{_NAME} {_TOK}'",
                f"bash -icx '{_NAME} {_TOK}'",
                f"bash --command '{_NAME} {_TOK}'",
                f"$(which bash) -c '{_NAME} {_TOK}'",
            ),
            # With NO space after the `-c`, the payload rides INSIDE the flag token
            # once shlex strips the quotes (`-c'<mint>'` -> one token).  The bare-flag
            # pattern rejects a token carrying the payload's own characters, so the
            # glued spelling was examined by NO consumer of the shared extractor --
            # this floor included.
            *_named(
                "test_glued_shell_flag_payload_still_blocked",
                f"bash -c'{_NAME} {_TOK}'",  # glued single-quoted
                f'bash -c"{_NAME} {_TOK}"',  # glued double-quoted
                f"sh -ec'{_NAME} {_TOK}'",  # letters before the c in the cluster
                f"bash -lc'{_NAME} {_TOK}'",
                f"sh -c'{_NAME} >/tmp/o {_TOK}'",  # redirect form needs the descent
            ),
            # With NO space before the redirect, the tokenizer keeps it glued to its
            # neighbour as one word, so a program (or verb) comparison against that
            # word fails.  bash splits the redirect off before exec, so the comparison
            # does too.
            *_named(
                "test_attached_redirect_still_blocked",
                f"{_NAME}>/tmp/out {_TOK}",
                f"{_NAME}>>/tmp/out {_TOK}",
                f"{_NAME} {_TOK}>/tmp/out",
                f"{_NAME}>/tmp/out {_TOK} --ttl 1h",
            ),
            # `shlex` splits on WHITESPACE only, so `true;<name>` arrives as one word
            # and a program comparison against it matches nothing.  bash runs whatever
            # follows the operator, so the program is taken from the trailing segment.
            *_named(
                "test_glued_control_operator_still_blocked",
                f"true;{_NAME}>/tmp/minted {_TOK}",
                f"true;{_NAME} {_TOK}",
                f"echo hi&&{_NAME} {_TOK}",
                f"echo hi||{_NAME} {_TOK}",
                f"true;/usr/local/bin/{_NAME} {_TOK}",
                f"x|{_NAME} {_TOK}",
            ),
            # `${X:-<name>}` hands the shell a runnable program name without the name
            # ever appearing bare, so the LITERAL branch is resolved before comparing.
            # A variable-only expansion (`$X`) carries no literal and is not resolved
            # here -- that case belongs to the raw-text half of the union.
            *_named(
                "test_literal_parameter_expansion_default_still_blocked",
                f"unset X; ${{X:-{_NAME}}}>/tmp/x {_TOK}",
                f"unset X; ${{X:-{_NAME}}} {_TOK}",
                f"${{X:+{_NAME}}} {_TOK}",
                f"${{X-{_NAME}}} {_TOK}",
            ),
            # A wrapper and a redirect INTERLEAVE.  With the redirect glued on, the
            # substitution's closing paren is not word-final, so peeling the
            # wrapper first leaves that paren in place and the program comparison
            # fails; peeling the redirect first breaks the plain glued form instead.
            # The peel runs to a fixed point, so neither order can hide the program.
            *_named(
                "test_attached_redirect_on_substitution_program_still_blocked",
                f"$(which {_NAME})>/tmp/out {_TOK}",
                f"$(which {_NAME})>>/tmp/out {_TOK}",
                f"`which {_NAME}`>/tmp/out {_TOK}",
                f'"$(command -v {_NAME})">/tmp/out {_TOK}',
            ),
            # An UNQUOTED substitution is split on its own spaces by the shell-word
            # splitter, so the resolved program already lands in a word of its own.  A
            # QUOTED one arrives as one multi-word word instead; a resolver's final
            # argument IS the program it resolves to, so the last word is compared.
            *_named(
                "test_quoted_substitution_body_resolves_to_its_program",
                f'"$(command -v {_NAME})" {_TOK}',
                f'"`command -v {_NAME}`" {_TOK}',
            ),
        ],
    )
    def test_mint_spellings_still_blocked(self, origin, cmd):
        assert _denied_by(cmd) == _RULE_MINT

    def test_hyphenated_spelling_now_blocked(self):
        # NEW COVERAGE: the retired pattern hardcoded the unhyphenated name, so
        # this real invocation form was allowed.
        assert _denied_by(f"{_HYPH} {_TOK} --ttl 30m") == _RULE_MINT

    @pytest.mark.parametrize(
        ("origin", "cmd"),
        [
            # Descending into the payload must not make the payload's own false
            # positives reappear -- the same argv rules apply one level down.
            *_named(
                "test_nested_shell_payload_incidental_mention_allowed",
                f'bash -c "cd /workplace/alice/{_NAME}-wt-x && pytest test/test_{_TOK}_auth.py"',
                f'sh -c "ls /workplace/alice/{_NAME}-wt-x"',
                f'bash -c "{_NAME} doctor | grep {_TOK}"',
            ),
            # Splitting on the operator must not turn a product-named PATH into a
            # program: the trailing segment still has to BE the CLI.
            *_named(
                "test_glued_control_operator_incidental_mention_allowed",
                f"true;ls /workplace/alice/{_NAME}-wt-x",
                f"echo hi&&cat /workplace/alice/{_NAME}-wt-x/docs/{_TOK}.md",
                f"cd /workplace/alice/{_NAME}-wt-x;pytest test/test_{_TOK}_auth.py",
                f"true;{_NAME} doctor | grep {_TOK}",
            ),
            # Taking a substitution body's last word must stay confined to the body:
            # these resolve to `cat` and `pytest`, and the product name appears only in
            # an ordinary path argument, which is the false positive being removed.
            *_named(
                "test_substitution_peel_does_not_reach_into_arguments",
                f"$(which cat) /workplace/alice/{_NAME}-wt-x/docs/{_TOK}.md",
                f'cd /workplace/x/{_NAME}-wt-y && "$(command -v pytest)" test/test_{_TOK}_auth.py',
                f"$(which cat) /workplace/alice/{_NAME}-wt-x/out>/tmp/copy",
            ),
            *_named(
                "test_incidental_mention_allowed",
                f"cd /workplace/x/{_NAME}-wt-y && pytest test/test_{_TOK}_auth.py",
                f"cd /workplace/x/{_NAME}-wt-y && grep -n mint src/kiro_crew/{_TOK}_auth.py",
                f"tail -20 /tmp/{_NAME}-gw.log | sed 's/{_TOK}=.*/REDACTED/'",
                f"KIROCREW_HOME=/tmp/h ./bin/{_NAME} gateway  # banner prints the auth {_TOK}",
                f"cat /tmp/{_NAME}-dev/config.json  # contains a {_TOK} field",
                f"grep -rn {_TOK} ~/.{_NAME}/skills/",
                f"{_NAME} doctor 2>&1 | grep {_TOK}",
                f"cd ~/.{_NAME} && cat {_TOK}.txt",
                f"ls /tmp/{_NAME}-dev/{_TOK}s",
                f"{_NAME} doctor > /tmp/{_TOK}.log",
                f"cat /tmp/{_NAME}-dev/{_TOK}_cache.json",
                f"ls ~/.{_NAME}/skills/ && cat {_TOK}s.md",
            ),
        ],
    )
    def test_mint_mention_outside_the_invocation_allowed(self, origin, cmd):
        assert _denied_by(cmd) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            f"bash -c -- '{_NAME} >/tmp/x {_TOK}'",
            f"sh -c -- '{_NAME} {_TOK}'",
            f"bash -c -- -- '{_NAME} {_TOK}'",
            f"bash -xc -- '{_NAME} >/tmp/x {_TOK}'",
            f"bash -c -- 'pkill -f {_NAME}'",
        ],
    )
    def test_double_dash_before_payload_still_blocked(self, cmd):
        # `--` ends option parsing, so the script is the token AFTER it.  Taking
        # `-c`'s immediate neighbour picked up `--` itself and inspected nothing.
        assert _denied_by(cmd) is not None

    def test_nesting_deeper_than_any_cap_still_blocked(self):
        """Four-plus wrappers must not outrun the payload walk.

        A numeric depth cap is itself a bypass -- whatever the number, one more
        wrapper defeats it.  The walk is bounded structurally instead (a payload is
        strictly shorter than its parent's source), so it descends arbitrarily
        deep.  The redirect form is used on purpose: the raw-text half of the union
        cannot match it, so only the descent can catch this.
        """
        inner = f"{_NAME} >/tmp/m {_TOK}"
        for _ in range(4):
            inner = "bash -c " + repr(inner)
        assert _denied_by(inner) == _RULE_MINT

    # --- incidental mentions: allowed ---

    def test_word_order_no_longer_decides_the_verdict(self):
        # An order-sensitive pattern would give the same intent opposite verdicts
        # purely by which word came first.  Both spellings of one benign command
        # must agree.
        under_path = f"cd /workplace/x/{_NAME}-wt-y && grep {_TOK}_auth.py"
        mentioned_after = f"grep {_TOK}_auth.py  # in a {_NAME} worktree"
        assert _denied_by(under_path) is None
        assert _denied_by(mentioned_after) is None
