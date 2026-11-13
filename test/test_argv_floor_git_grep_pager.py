"""A git command cannot open or configure a pager, which would run a program.

`git grep -O<pager>` runs the pager on the matching files, and the pager a git
command uses can be set from the command line or the environment. The pager is a
program git runs, and the agent controls which program, so every one of these is a
route to run an arbitrary command through git. The self-kill floor refuses the whole
operation, not a judged pager value: the pager name is swappable (an alias, a `PATH`
shadow, a `LESSOPEN` hook), so no pager value is benign.

Refused: `git grep -O` / `--open-files-in-pager` in every spelling, and any git
invocation that sets the pager by config to a program (`-c core.pager=`, `-c
pager.<cmd>=`, `--config-env=`, `git config core.pager <prog>`, `GIT_PAGER=`, `PAGER=`,
`GIT_CONFIG_PARAMETERS=`, the `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n` env trio), including
behind an execution-prefix wrapper (`command`/`env`/`sudo`/`nice`/...) or an env export
on the same line. Allowed: a plain search, `git --no-pager` / `-P`, and the no-op pager
forms -- an empty or `cat` pager value and a git-false `pager.<cmd>` -- which run no
distinct program. The value is read in git's grammar, so a subcommand option
(`git grep -c` count, `-e` pattern) is not mistaken for a git-global pager control.

Every self-kill attack string is built from parts so no single literal here spells the
kill command.
"""

from __future__ import annotations

import time

import pytest

from kiro_crew import security

_KILL = "self-protection-kill"
_PAGER = "self-protection-git-pager"
_K = "pk" + "ill"
_TARGET = "kirocrew"
_FILE = "test/test_argv_floor_inline_and_brace_scope.py"


def _verdict(cmd: str) -> "str | None":
    return security.is_denied(cmd)


def _denied(cmd: str) -> bool:
    return bool(_verdict(cmd))


def _is_kill(cmd: str) -> bool:
    reason = _verdict(cmd)
    return bool(reason) and _KILL in str(reason)


# ---- git grep -O / --open-files-in-pager, every spelling, is refused -----------------
# The pager value is irrelevant: a swappable name is never benign.


@pytest.mark.parametrize(
    "cmd",
    [
        "git grep -O less -- '*'",
        "git grep -Oless -- '*'",
        "git grep -O'less' -- '*'",
        "git grep -nOcat -e foo -- '*'",
        "git grep --open-files-in-pager=less -- '*'",
        "git grep --open-files-in-pager less -- '*'",
        "git grep --open=less -- '*'",
        "git grep -e foo -O cat -- README.md",
    ],
)
def test_git_grep_opening_a_pager_is_refused(cmd: str) -> None:
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


# ---- any route that sets the pager by config is refused ------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "git -c core.pager=less grep foo -- '*'",
        "git -c pager.grep=less grep foo -- '*'",
        "git -ccore.pager=less grep foo",
        "git --config-env=core.pager=MYVAR grep foo -- '*'",
        "git --config-env=pager.grep=MYVAR grep foo",
        "GIT_PAGER=less git grep foo -- '*'",
        "PAGER=less git grep foo -- '*'",
        "GIT_CONFIG_PARAMETERS=\"'core.pager=less'\" git grep foo -- '*'",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.pager GIT_CONFIG_VALUE_0=less git grep foo",
        # `pager.<cmd>=true` turns paging ON (the default pager runs), so it is refused
        # even though it names no program; a bare key with no value is `=true` the same way.
        "git -c pager.grep=true grep foo -- '*'",
        "git -c core.pager grep foo -- '*'",
    ],
)
def test_git_setting_the_pager_by_config_is_refused(cmd: str) -> None:
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


# ---- a git-global option is read in git's grammar, not flat: an option that belongs to
# the SUBCOMMAND (grep's `-c` count, grep's `-P` Perl-regexp) is not a git-global pager
# control, and a git-global `-c core.pager=<prog>` set before the subcommand is not
# cancelled by the subcommand's own `-P`. -------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        # grep's own `-c` (count) takes no config operand, so the pattern after it
        # (`core.pager`, `pager.grep`) is data, not a git-global pager config.
        "git grep -c core.pager -- '*.md'",
        "git grep -c pager.grep",
        # grep's `-e` consumes the next token as its pattern, so a following `-O` /
        # `--open` is that pattern, not the pager option.
        "git grep -e -O -- README.md",
        "git grep -e --open -- README.md",
    ],
)
def test_a_subcommand_option_is_not_read_as_git_global(cmd: str) -> None:
    assert not _denied(cmd), _verdict(cmd)


# ---- a git config WRITER of a pager key is refused in every grammar: the legacy
# `git config <key> <value>`, the git 2.46+ `git config set <key> <value>`, and behind a
# git global (`-C`, `--no-pager`, `-c`) that the subcommand scan must step over first. ----


@pytest.mark.parametrize(
    "cmd",
    [
        "git config core.pager less",
        "git config set core.pager ./p",
        "git config set --global core.pager ./p",
        "git config set pager.log ./p",
        "git -C . config core.pager ./p",
        "git --no-pager config core.pager ./p",
        "git -c x.y=z config pager.log ./p",
        # a git global that takes a separate-token operand must not be read as the
        # subcommand, so grep's -O behind it is still seen.
        "git --attr-source HEAD grep -O./p -e foo -- file",
        "git --config-env core.pager=EV log",
    ],
)
def test_a_git_config_pager_writer_is_refused_in_every_grammar(cmd: str) -> None:
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


# ---- a git config READ / unset / list of a pager key stays allowed, including the
# `config set`-grammar verbs and a trailing redirect / pipe / `||` (it writes nothing). --


@pytest.mark.parametrize(
    "cmd",
    [
        "git config core.pager",
        "git config get core.pager",
        "git config unset core.pager",
        "git config list",
        "git config set core.pager cat",
        "git config core.pager 2>/dev/null || echo unset",
        "git config core.pager || true",
        "git config pager.branch | cat",
        "git config core.pager > /tmp/pager-setting.txt",
    ],
)
def test_a_git_config_pager_read_stays_allowed(cmd: str) -> None:
    assert not _denied(cmd), _verdict(cmd)


# ---- a quoted separator in an operand does NOT split the command, so a later `-O` is
# still examined; an unquoted subshell body is still reached. (Boundaries are taken before
# operands are dequoted, reusing the quote-aware segment splitter.) ---------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "git grep -e ';' -O./p -- file",
        "git grep -e 'x|y' -O./p -- file",
        "git grep -e '#' -O./p -- file",
        "git grep -e '(' -O./p -- file",
        "git grep -e '&' -O./p -- file",
        "git config -f '&' core.pager ./p",
        "git -c x.y=';' grep -O./p -- file",
        "( git grep -O less -- x )",
        # a real background `&` DOES separate two commands, so the git after it is seen.
        "foo & git grep -O less -- x",
    ],
)
def test_a_quoted_separator_does_not_hide_a_later_pager_option(cmd: str) -> None:
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "g=git; $g grep -O./pager-script -e Rules -- AGENTS.md",
    ],
)
def test_a_variable_assembled_git_program_is_still_caught(cmd: str) -> None:
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


def test_a_config_set_value_operand_does_not_hide_the_write() -> None:
    # `git config set --value <pattern> <key> <value>`: the `--value` operand must be
    # consumed so `core.pager ./pager-script` is still read as the pager write.
    cmd = "git config set --value '.*' core.pager ./pager-script"
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


# ---- the GIT_CONFIG_COUNT/KEY_n/VALUE_n env trio pins config like `-c`: a no-op pin
# (cat / empty / a git-false pager.<cmd>) stays allowed -- the product's update-governance
# and git_gateway use it -- while a program value is refused. ---------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.pager GIT_CONFIG_VALUE_0=cat git log -5",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=pager.log GIT_CONFIG_VALUE_0=false git log",
        "GIT_CONFIG_KEY_0=core.pager GIT_CONFIG_VALUE_0=/usr/bin/cat git log",
    ],
)
def test_an_env_config_trio_noop_pin_is_allowed(cmd: str) -> None:
    assert not _denied(cmd), _verdict(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.pager GIT_CONFIG_VALUE_0=less git grep foo -- x",
        "GIT_CONFIG_KEY_0=core.pager git log",  # a pager key with no paired value
    ],
)
def test_an_env_config_trio_program_or_unpaired_key_is_refused(cmd: str) -> None:
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


# ---- a core.pager / GIT_PAGER set to a TRUSTED absolute path to cat is the no-op the
# product's update-governance uses, so it stays allowed; an attacker-controlled */cat
# (e.g. /tmp/x/cat) is still refused. --------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "git -c core.pager=/usr/bin/cat log --oneline -5",
        "git -c core.pager=/bin/cat log",
        "GIT_PAGER=/usr/bin/cat git log",
        "git config core.pager /usr/bin/cat",
    ],
)
def test_a_trusted_absolute_cat_pager_is_allowed(cmd: str) -> None:
    assert not _denied(cmd), _verdict(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "git -c core.pager=/tmp/evil/cat log",
        "git -c core.pager=./cat log",
        "GIT_PAGER=/home/x/cat git log",
    ],
)
def test_an_untrusted_cat_path_pager_is_refused(cmd: str) -> None:
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


def test_a_real_separator_after_an_allowed_config_write_is_split() -> None:
    # The real `;` splits; the no-op `core.pager=cat` write and the plain `git log` are
    # both allowed, so the whole line is allowed.
    assert not _denied("git config core.pager cat; git log"), _verdict(
        "git config core.pager cat; git log"
    )


# ---- repeated pagination switches and repeated -c for one key are evaluated LAST-wins. --


@pytest.mark.parametrize(
    "cmd",
    [
        "git -P -p -c core.pager=./p log",
        "git --no-pager --paginate -c core.pager=./p log",
        "git -c core.pager=cat -c core.pager=less log",
    ],
)
def test_last_pagination_or_config_value_wins_refused(cmd: str) -> None:
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "git -c core.pager=less -c core.pager=cat log",
        "git -p -P -c core.pager=less log",
        "GIT_PAGER=less GIT_PAGER=cat git log",
    ],
)
def test_last_pagination_or_config_value_wins_allowed(cmd: str) -> None:
    assert not _denied(cmd), _verdict(cmd)


def test_the_floor_tracks_every_git_global_value_option() -> None:
    # The floor's git-global value-option set mirrors argv_floor_git._GIT_ARG_FLAGS but is
    # defined locally (argv_floor_self is the base argv-floor layer and may not import the
    # higher argv_floor_git). This pins the mirror: every value option must be SKIPPED, so
    # a real `-c core.pager=<prog>` after it is still caught -- otherwise the flag's value
    # would be read as the subcommand and the config route missed.
    from kiro_crew.security.argv_floor_git import _GIT_ARG_FLAGS

    for flag in _GIT_ARG_FLAGS:
        if flag == "-c":
            continue  # -c has its own config-aware branch, exercised elsewhere
        cmd = f"git {flag} somevalue -c core.pager=less log"
        assert _denied(cmd), cmd
        assert _PAGER in str(_verdict(cmd)), cmd


def test_a_git_global_pager_config_is_not_cancelled_by_a_subcommand_flag() -> None:
    # `-c core.pager=./pager-script` is a git-GLOBAL config naming a program; `log -P`
    # is the log subcommand's own `-P`, which does not cancel the global pager route.
    cmd = "git -c core.pager=./pager-script log -P -1"
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


# ---- a pager cannot slip past the floor behind an execution-prefix wrapper, an env
# assignment exported on the same line, a `git config` writer, or an empty substitution
# that hides the `git` substring from the cheap gate. --------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        # execution-prefix wrappers before git: the wrapped git still opens the pager.
        "command git grep -O less -- x",
        "exec git grep -O less -- x",
        "nice git grep -O less -- x",
        "nice -n 10 git grep -O less -- x",
        "sudo git grep -O less -- x",
        "env git grep -O less -- x",
        # the full shared wrapper table is honoured (reused from argv_rules), not a
        # partial inline copy: a wrapper argv_rules knows is not a bypass here.
        "doas git grep -O less -- x",
        "pkexec git grep -O less -- x",
        "runuser -u root git grep -O less -- x",
        "chroot /newroot git grep -O less -- x",
        "flock /tmp/lock git grep -O less -- x",
        "xargs git grep -O less -- x",
        # env / sudo carry their own NAME=value words, which set the pager.
        "env GIT_PAGER=less git grep foo -- x",
        # an env pager EXPORTED by an earlier command on the same line.
        "export GIT_PAGER=less; git grep foo -- x",
        # `git config core.pager <program>` WRITES the pager into a config file.
        "git config core.pager less",
        "git config --global core.pager /tmp/p",
        "git config pager.grep less",
        # an empty substitution hides the `git` substring but resolves to git.
        "gi$()t grep -Oless -- x",
    ],
)
def test_a_pager_behind_a_wrapper_or_writer_is_refused(cmd: str) -> None:
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


# ---- --no-pager / -P turns OUTPUT paging off, but never cancels `grep -O`: -O opens
# the matching files in a program whatever the output paging does. --------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "git --no-pager grep -O less -- '*'",
        "git -P grep -Ocat -- '*'",
        "git --no-pager grep --open-files-in-pager=less -- '*'",
    ],
)
def test_no_pager_does_not_cancel_grep_dash_o(cmd: str) -> None:
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


# ---- a pager opened/configured inside a nested payload is refused --------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "eval 'git grep -O less -- x'",
        "sh -c 'git -c core.pager=less grep foo -- x'",
        "bash -c 'GIT_PAGER=less git grep foo -- x'",
    ],
)
def test_a_nested_git_pager_is_refused(cmd: str) -> None:
    assert _denied(cmd), cmd


# ---- turning the pager off, and a plain search, stay allowed -------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "git --no-pager grep foo -- '*.py'",
        "git -P grep foo -- '*.py'",
        "git grep foo -- '*.py'",
        "git grep -n -e foo -- ':!*.lock' '*'",
        "git log --oneline -5",
        # `-O` after the `--` operand terminator is a pathspec, not the pager option.
        "git grep -e foo -- README.md -O cat",
        # Lowercase `-o` prints matching text and runs no pager -- it is NOT `-O`.
        "git grep -o -e Kiro -- README.md",
        "git grep -no x",
        # An attached operand (`-e<pat>`) ends the short-option cluster, so an `O` in
        # the pattern is not read as the pager option.
        "git grep -eKiro",
        "git grep -eKirO -- README.md",
    ],
)
def test_a_pager_free_git_command_is_allowed(cmd: str) -> None:
    assert not _denied(cmd), _verdict(cmd)


# ---- the no-program pager forms are allowed: a route runs a pager only when it NAMES
# a distinct program, so an empty / `cat` / git-false / disabled pager setting is not
# refused. Every OTHER named program is still refused (the name is swappable). ---------


@pytest.mark.parametrize(
    "cmd",
    [
        # core.pager / GIT_PAGER / PAGER set to the empty string: no program.
        "git -c core.pager= log",
        "GIT_PAGER= git grep foo -- x",
        "PAGER= git log",
        # core.pager / GIT_PAGER / PAGER set to `cat`: the no-op pager the liveness
        # advice proposes and the product's own git calls use, so it stays allowed.
        "git -c core.pager=cat log",
        "git -c core.pager=cat grep foo -- '*'",
        "GIT_PAGER=cat git grep foo -- x",
        "PAGER=cat git log",
        "git -c pager.grep=cat grep foo -- x",
        # `git config` that READS, UNSETS, or writes a no-op pager is allowed.
        "git config --get core.pager",
        "git config core.pager cat",
        "git config --unset core.pager",
        "git config pager.grep false",
        # pager.<cmd> set to a git false: paging off, no program.
        "git -c pager.grep=false grep -n -e Kiro -- README.md",
        "git -c pager.grep=off grep foo -- x",
        "git -c pager.grep=0 grep foo -- x",
        # --no-pager / -P cancels a pager-config route (the pager never runs).
        "git -c core.pager=cat --no-pager log",
        "git --no-pager -c core.pager=less log",
        "git -P -c core.pager=cat log",
        "GIT_PAGER=less git --no-pager log",
    ],
)
def test_the_no_program_pager_forms_are_allowed(cmd: str) -> None:
    assert not _denied(cmd), _verdict(cmd)


# ---- the four self-kill bypass classes stay refused (regression guards) --------------
# There is no operand exemption, so each of these is refused. They pin that a future
# `git grep` data exemption must not reopen any of them.


@pytest.mark.parametrize(
    "cmd",
    [
        f"git grep -e '*' -e {_TARGET} -e ';' {_K} -f {_TARGET} -- {_FILE}",
        f'git(){{ shift; "$@"; }}; git grep {_K} -f {_TARGET}',
        f'function git {{ shift; "$@"; }}; git grep {_K} -f {_TARGET}',
        f"PATH=/tmp:$PATH; git grep {_K} -f {_TARGET}",
        f"hash -p /tmp/shim/git git; git grep {_K} -f {_TARGET}",
        f"command git grep {_K} -f {_TARGET}",
        f"true && git grep {_K} -f {_TARGET}",
        f"/tmp/git grep {_K} -f {_TARGET}",
        f"./git grep {_K} -f {_TARGET}",
        f"git(){{ shift; \"$@\"; }}; eval 'git grep {_K} -f {_TARGET}'",
        f"git(){{ shift; \"$@\"; }}; sh -c 'git grep {_K} -f {_TARGET}'",
    ],
)
def test_a_git_grep_operand_kill_stays_refused(cmd: str) -> None:
    assert _is_kill(cmd), cmd


# ---- a ``#`` line comment is text the shell drops, not a command. An annotated no-op
# (the form the product's own liveness advice invites) and a trailing comment must not
# have their words read as a pager route. A comment can only start outside quotes, so a
# quoted ``#`` pattern and an escaped ``\#`` are NOT comments and still parse. ----------


@pytest.mark.parametrize(
    "cmd",
    [
        # the comment names a pager, the real command pins the no-op cat -- allowed
        "# git -c core.pager=less would block on a tty; use cat instead\n"
        "git -c core.pager=cat log --oneline -5",
        "# use cat\ngit -c core.pager=cat log",
        # a trailing comment the shell drops to end of line
        "git grep foo # -O -- x",
        "git log # core.pager=less is bad",
        # a comment with an apostrophe must not corrupt reading the real command
        "# don't use less, it'll hang\ngit -c core.pager=cat log",
    ],
)
def test_a_line_comment_is_not_read_as_a_pager_route(cmd: str) -> None:
    assert not _denied(cmd), _verdict(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        # the comment is on line one; the REAL command on line two sets a program pager
        "# use cat instead\ngit -c core.pager=less log",
        # a quoted ``#`` is a grep pattern, not a comment, so the later -O still opens
        "git grep -e '#' -O -- x",
        # an escaped ``\\#`` is a literal operand, not a comment, so -O still opens
        "git grep -e \\# -O -- x",
    ],
)
def test_a_non_comment_hash_still_parses_a_pager_route(cmd: str) -> None:
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


# ---- forms the security-scope harness confirmed as legitimate: a here-document body fed
# to a non-shell program is data, git honours GIT_PAGER and core.pager over PAGER,
# GIT_CONFIG_PARAMETERS is read entry by entry, and Git for Windows' own cat is the
# trusted absolute cat on that platform. ------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "git commit -m \"$(cat <<'EOF'\nfix(security): refuse git grep -O less and git -c "
        "core.pager=less log\n\nGIT_PAGER=less git log hangs a session; use git --no-pager "
        'log.\nEOF\n)"',
        "cat <<'EOF'\nGIT_PAGER=less git log\nEOF",
        "PAGER=less GIT_PAGER=cat git log --oneline -5",
        "PAGER=less git -c core.pager=cat log --oneline -5",
        "PAGER=less GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.pager GIT_CONFIG_VALUE_0=cat git log",
        "GIT_CONFIG_PARAMETERS=\"'color.ui=never'\" git status",
        "GIT_CONFIG_PARAMETERS=\"'core.pager=cat' 'color.ui=never'\" git log",
        'git -c core.pager="C:\\Program Files\\Git\\usr\\bin\\cat.exe" log --oneline -5',
        'git -c core.pager="C:/Program Files/Git/usr/bin/cat.EXE" log',
        # a newline inside a quoted commit message is data, not a command boundary
        "git commit -m 'fix(security): refuse git grep -O less\n\n"
        "GIT_PAGER=less git log hangs a session'",
        # pager.<sub> set to a git false turns this command's paging off
        "PAGER=less git -c pager.log=false log --oneline -5",
        # a subcommand git never pages does not use PAGER
        "export EDITOR=vim PAGER=less; git rev-parse HEAD",
        "PAGER=less git status",
        "git -c core.pager=cat.exe log --oneline -5",
        'git -c core.pager="C:\\Program Files (x86)\\Git\\usr\\bin\\cat.exe" log --oneline -5',
    ],
)
def test_a_confirmed_legitimate_pager_form_is_allowed(cmd: str) -> None:
    assert not _denied(cmd), _verdict(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        # a here-document fed to a shell RUNS its body
        "bash <<'EOF'\ngit grep -O less -- x\nEOF",
        # the command after the delimiter line is a real command
        "cat <<'EOF'\nhello\nEOF\ngit grep -O less -- x",
        # no delimiter line, so nothing is dropped
        "cat <<'EOF'\nGIT_PAGER=less git log",
        "PAGER=less git log",
        # GIT_PAGER outranks both PAGER and core.pager
        "GIT_PAGER=less PAGER=cat git log",
        "GIT_PAGER=less git -c core.pager=cat log",
        "GIT_CONFIG_PARAMETERS=\"'core.pager=less'\" git log",
        "GIT_CONFIG_PARAMETERS=\"'core.pager'='less'\" git log",
        'GIT_CONFIG_PARAMETERS="\'unterminated" git log',
        'git -c core.pager="C:\\tmp\\cat.exe" log',
        # -p forces paging even for a subcommand that never pages
        "PAGER=less git -p rev-parse HEAD",
        # an unknown word may be a paging alias
        "PAGER=less git lg",
        # pager.<other> does not turn off this command's paging
        "PAGER=less git -c pager.status=false log",
        "export PAGER=less; git log",
        # a config pager runs whatever the subcommand
        "git -c core.pager=less rev-parse HEAD",
        # an unquoted newline still separates commands
        "git commit -m 'x'\ngit grep -O less -- y",
        "sh -c 'true\ngit grep -O less -- y'",
        'git -c core.pager="C:\\Program Files (x86)\\evil\\cat.exe" log',
        "git -c core.pager=evil.exe log",
    ],
)
def test_a_real_pager_route_near_those_forms_is_still_refused(cmd: str) -> None:
    assert _denied(cmd), cmd
    assert _PAGER in str(_verdict(cmd)), cmd


# ---- the floor stays LINEAR in the length of the command it reads. The dropped
# carry-prefix pass re-tokenised a growing prefix per segment, which is quadratic in the
# segment count; a long command of harmless segments followed by a real pager route must
# still be read in time that grows with the command, not with its square. ---------------


def _long_pager_command(segments: int) -> str:
    # `segments` harmless no-ops, a `;` between each, then one real pager route at the end
    # so the floor walks every segment before it decides.
    return "; ".join(["true"] * segments + ["git grep -O less -- x"])


def test_the_pager_floor_stays_linear_on_a_long_command() -> None:
    small = _long_pager_command(2_000)
    large = _long_pager_command(10_000)  # 5x the segments

    # Correctness is not sacrificed for length: the trailing route is still refused.
    assert _PAGER in str(_verdict(large)), "a pager route after many segments is missed"

    def _elapsed(cmd: str) -> float:
        best = float("inf")
        for _ in range(3):  # take the best of a few runs to shed scheduler noise
            start = time.perf_counter()
            _verdict(cmd)
            best = min(best, time.perf_counter() - start)
        return best

    t_small = _elapsed(small)
    t_large = _elapsed(large)

    # Absolute ceiling: a quadratic walk of 10k segments would take seconds; linear is
    # milliseconds. The budget is deliberately loose so a loaded CI host does not flake.
    assert t_large < 5.0, f"10k-segment command took {t_large:.3f}s (expected linear)"

    # Scaling: 5x the segments should cost roughly 5x, not 25x. Allow generous slack for
    # a tiny baseline and timer noise; a quadratic regression (~25x) still trips it.
    baseline = max(t_small, 1e-4)
    assert t_large < baseline * 12, (
        f"time grew {t_large / baseline:.1f}x for 5x the segments "
        f"(small={t_small:.4f}s large={t_large:.4f}s); expected ~linear"
    )
