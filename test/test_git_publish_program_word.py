"""The git-publish floor reads the PROGRAM word of every command it can reach.

A publish whose program word the shell resolves at run time -- a quoted
expansion (``"$G"``), a path around one (``"/usr/bin/$G"``), a glob that can
name git (``g?t``), a substitution split across words -- is unverifiable, and
the floor denies it as such wherever it sits: at the top of the line, after a
newline or a glued separator, inside a compound construct (``if``/``while``/
``for`` bodies, ``case`` arms, subshells, brace groups, function bodies),
behind a precommand word and its options, or in a nested shell payload.

The same command cannot borrow a verdict from a feature push elsewhere on the
line: not from a sibling command, and not from a feature push carried among its
own arguments.

Commands that only MENTION a push, or run a runtime-resolved program with some
other subcommand, stay allowed.
"""

import pytest

from kiro_crew.security import _is_git_publish, _is_push_to_protected_branch, is_denied

# "pus" + "h" keeps a literal blocked command out of the test source.
P = "pus" + "h"
GIT_FEATURE = f"git {P} origin feature"
UNVERIFIABLE = "git-publish-target-unverifiable"

UNVERIFIABLE_PROGRAM_WORDS = [
    f'"$G" {P} origin main',
    f'"${{G}}" {P} origin main',
    f'"/usr/bin/$G" {P} origin main',
    f"g?t {P} origin main",
    f"/usr/bin/g?t {P} origin main",
    f"/usr/bin/[g]it {P} origin main",
    f'"$G" -C . {P} origin main',
    f'"$G" {P}',
    f'"$G" {P} origin HEAD:main',
    f'"$G" {P} origin feature',
    f"$(echo git) {P} origin main",
    f"`echo git` {P} origin main",
    f'G=git "$G" {P} origin main',
]

LINE_SEPARATORS = [
    f'echo ok\n"$G" {P} origin main',
    f'set -e\nG=git\n"$G" {P} origin main',
    f'x=1;"$G" {P} origin main',
    f'cd x;"$G" {P} origin main',
    f'cd x&&"$G" {P} origin main',
    f'cd x||"$G" {P} origin main',
    f'true|"$G" {P} origin main',
    f'true & "$G" {P} origin main',
    f'true &"$G" {P} origin main',
    f'true |& "$G" {P} origin main',
    f'"$G" \\\n{P} origin main',
    f'2>/dev/null "$G" {P} origin main',
    f'>out "$G" {P} origin main',
    f'"$G" 2>&1 {P} origin main',
    f'"$G" > log {P} origin main',
    "\"$G\" p'ush' origin main",
    f"{{git,}} {P} origin main",
]

PRECOMMANDS = [
    f'sudo "$G" {P} origin main',
    f'sudo -u root "$G" {P} origin main',
    f'env -i "$G" {P} origin main',
    f'env -u X "$G" {P} origin main',
    f'exec -a x "$G" {P} origin main',
    f'nice -n 5 "$G" {P} origin main',
    f'timeout 5 "$G" {P} origin main',
    f'setsid "$G" {P} origin main',
    f'ionice -c3 "$G" {P} origin main',
    f'stdbuf -o0 "$G" {P} origin main',
    f'xargs "$G" {P} origin main',
    f'xargs -I{{}} "$G" {P} origin main',
    f'coproc "$G" {P} origin main',
    f'command "$G" {P} origin main',
    f'time -p "$G" {P} origin main',
]

COMPOUND_CONSTRUCTS = [
    f'case x in x) "$G" {P} origin main;; esac',
    f'if true; then "$G" {P}; fi',
    f"if true; then $G {P} origin main; fi",
    f'if "$G" {P} origin main; then :; fi',
    f'if false; then :; else "$G" {P}; fi',
    f'for i in 1; do "$G" {P}; done',
    f'while true; do "$G" {P} origin main; done',
    f'until false; do "$G" {P} origin main; done',
    f'if true\nthen\n"$G" {P} origin main\nfi',
    f'( "$G" {P} origin main )',
    f'("$G" {P} origin main)',
    f'{{ "$G" {P} origin main; }}',
    f'f() {{ "$G" {P} origin main; }}; f',
    f'f(){{ "$G" {P} origin main;}};f',
    f'function f {{ "$G" {P} origin main; }}; f',
    f'case x in (x) "$G" {P} origin main;; esac',
    f'select x in a; do "$G" {P} origin main; done',
    f"if true; then `echo git` {P} origin main; fi",
    f'! "$G" {P} origin main',
    f'time "$G" {P} origin main',
]

SIBLING_FEATURE_PUSH = [
    f'"$G" {P} origin main ; {GIT_FEATURE}',
    f'"$G" {P} origin main && {GIT_FEATURE}',
    f'{GIT_FEATURE} ; "$G" {P} origin main',
    f'if true; then "$G" {P}; fi; {GIT_FEATURE}',
    f'case x in x) "$G" {P} origin main;; esac; {GIT_FEATURE}',
    f'( "$G" {P} origin main ); {GIT_FEATURE}',
    f'{{ "$G" {P} origin main; }} && {GIT_FEATURE}',
]

ARGUMENT_CARRIED_FEATURE_PUSH = [
    f'"$G" {P} origin main eval {GIT_FEATURE}',
    f"\"$G\" {P} origin main eval '{GIT_FEATURE}'",
    f"\"$G\" {P} eval '{GIT_FEATURE}'",
    f"\"$G\" {P} origin main bash -c '{GIT_FEATURE}'",
    f'"$G" {P} origin main <({GIT_FEATURE})',
]

NESTED_PAYLOADS = [
    f"bash -c '\"$G\" {P} origin main'",
    f"bash -c '\"$G\" {P} origin main' && {GIT_FEATURE}",
    f"eval '\"$G\" {P} origin main'",
    f"sh -c 'if true; then \"$G\" {P}; fi'",
]

STILL_ALLOWED = [
    GIT_FEATURE,
    f'"$G" stash {P}',
    '"$G" status',
    f'"$G" log --grep {P}',
    f'echo "$G" {P}',
    f"printf '%s\\n' \"$x\" {P}",
    f'printf \'%s\\n\' "$(date)" "$name" {P}',
    f'echo $(date) "$x" {P}',
    f'echo $(date +%s) "$x" {P}',
    f'echo "a;" "$x" {P}',
    f'echo "a)" "$G" {P} x',
    f'echo "(" "$x" {P} now',
    f"grep -e '{{' \"$f\" {P}",
    f'printf "%s|" "$x" {P}',
    f'echo do "$X" {P} y',
    f'"$PAGER" {P}.txt',
    f"make {P}",
    f"echo {P}",
    '"$EDITOR" file.txt',
    "$CC -o x x.c",
    'for f in *.py; do black "$f"; done',
    f'cd "$dir" && {GIT_FEATURE}',
    f'if [ -n "$X" ]; then {GIT_FEATURE}; fi',
    f"nice -n 5 {GIT_FEATURE}",
    f"bash -c '{GIT_FEATURE}'",
    f"bash -c 'if true; then {GIT_FEATURE}; fi'",
    f"'git' {P} origin feature",
    f"sleep 1 & {GIT_FEATURE}",
    f'"$G" status 2>&1 | grep {P}',
    f'echo $(cat a b) "$x" {P}',
    'case "$1" in a) echo a;; esac',
    f"time {GIT_FEATURE}",
]


def _assert_unverifiable(command: str) -> None:
    reason = is_denied(command)
    assert reason is not None, command
    assert UNVERIFIABLE in reason, reason


@pytest.mark.parametrize("command", UNVERIFIABLE_PROGRAM_WORDS)
def test_a_runtime_resolved_program_word_is_an_unverifiable_publish(command: str) -> None:
    assert _is_git_publish(command.lower()) is True
    assert _is_push_to_protected_branch(command.lower()) is True
    _assert_unverifiable(command)


@pytest.mark.parametrize("command", LINE_SEPARATORS)
def test_a_newline_or_glued_separator_opens_a_program_position(command: str) -> None:
    _assert_unverifiable(command)


@pytest.mark.parametrize("command", PRECOMMANDS)
def test_a_precommand_and_its_options_do_not_hide_the_program_word(command: str) -> None:
    _assert_unverifiable(command)


@pytest.mark.parametrize("command", COMPOUND_CONSTRUCTS)
def test_a_publish_inside_a_compound_construct_is_judged(command: str) -> None:
    _assert_unverifiable(command)


@pytest.mark.parametrize("command", SIBLING_FEATURE_PUSH)
def test_a_sibling_feature_push_cannot_vouch_for_an_unverifiable_one(command: str) -> None:
    _assert_unverifiable(command)


@pytest.mark.parametrize("command", ARGUMENT_CARRIED_FEATURE_PUSH)
def test_a_feature_push_in_the_arguments_cannot_stand_in_for_the_target(command: str) -> None:
    assert _is_push_to_protected_branch(command.lower()) is True
    _assert_unverifiable(command)


@pytest.mark.parametrize("command", NESTED_PAYLOADS)
def test_a_nested_payload_with_an_unverifiable_program_is_denied(command: str) -> None:
    _assert_unverifiable(command)


@pytest.mark.parametrize("command", STILL_ALLOWED)
def test_commands_that_do_not_run_an_unverifiable_publish_stay_allowed(command: str) -> None:
    assert is_denied(command) is None
