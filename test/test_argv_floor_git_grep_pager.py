"""A ``git grep`` pager that kills the product is a self-kill.

``git grep`` runs a pager over the matching files, taken from ``-O`` /
``--open-files-in-pager``, a ``-c core.pager=`` / ``-c pager.grep=`` global
option, or ``GIT_PAGER`` / ``PAGER`` in the environment. The self-kill argv
floor reads ``pkill`` / ``killall`` only as a word of the command, so a kill
handed to git as a pager has to be read out of that option to be seen.

The pager is read the way git parses it, one invocation at a time and with the
case as submitted: a value option's value (``-e`` pattern, ``-f`` file, and the
rest of a cluster after such a letter) is never a pager, and a plain
``git grep`` with no pager still passes.
"""

from __future__ import annotations

import pytest

from kiro_crew import security
from kiro_crew.security import argv_floor_pager, argv_floor_self, shell_normalizer

_KILL = "self-protection-kill"
# Assembled from parts so no literal kill command appears in this file.
_K = "pk" + "ill"
_TARGET = "kirocrew"
_FILE = "src/kiro_crew/security/argv_floor_self.py"


def _verdict(cmd: str) -> str | None:
    return security.is_denied(cmd)


def _is_kill(cmd: str) -> bool:
    reason = _verdict(cmd)
    return bool(reason) and _KILL in str(reason)


# ---- the five pager forms: refused when the pager kills the product --------------


@pytest.mark.parametrize(
    "cmd",
    [
        # 1) -O placed after the pathspec.
        f"git grep -e x -- {_FILE} -O'{_K} -f {_TARGET} #'",
        # 2) a clustered short option.
        f"git grep -nO'{_K} -f {_TARGET} #' -e '*;' -- {_FILE}",
        # 3) a global pager option before grep.
        f"git -c core.pager='{_K} -f {_TARGET}' grep -e '*;' -- {_FILE}",
        # 4) a pager from the environment.
        f"GIT_PAGER='{_K} -f {_TARGET}' git grep -e '*;' -- {_FILE}",
        # 5) an array expansion.
        f"a=('-O{_K} -f {_TARGET} #'); git grep \"${{a[@]}}\" -e '*;' -- {_FILE}",
    ],
)
def test_the_five_named_pager_forms_are_refused(cmd: str) -> None:
    assert _is_kill(cmd), cmd


@pytest.mark.parametrize("sep", [";", "|"])
@pytest.mark.parametrize(
    "form",
    [
        # 1) -O after the pathspec.
        "git grep -e 'a{sep}b' -e x -- {file} -O'{k} -f {t} #'",
        # 2) a clustered short option.
        "git grep -e 'a{sep}b' -nO'{k} -f {t} #' -- {file}",
        # 5) an array expansion.
        "a=('-O{k} -f {t} #'); git grep -e 'a{sep}b' \"${{a[@]}}\" -- {file}",
    ],
)
def test_a_quoted_separator_before_the_pager_does_not_hide_it(form: str, sep: str) -> None:
    cmd = form.format(sep=sep, file=_FILE, k=_K, t=_TARGET)
    assert _is_kill(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # The same pager read through spellings next to each named form.
        f"git grep -e x --open-files-in-pager='{_K} -f {_TARGET}' -- {_FILE}",
        f"git grep -e x --open='{_K} -f {_TARGET}' -- {_FILE}",
        f"git -c pager.grep='{_K} -f {_TARGET}' grep -e x -- {_FILE}",
        f"git -c Pager.Grep='{_K} -f {_TARGET}' grep -O -e x -- {_FILE}",
        # A later true pager.grep turns grep's paging back on, so core.pager runs.
        f"git -c pager.grep=false -c pager.grep=true -c core.pager='{_K} -f {_TARGET}'"
        f" grep -e x -- {_FILE}",
        f"PAGER='{_K} -f {_TARGET}' git grep -e x -- {_FILE}",
        f"a=(-n '-O{_K} -f {_TARGET} #'); git grep \"${{a[@]}}\" -e x -- {_FILE}",
        # -O clustered after -F / -E, which fold to the value letters -f / -e.
        f"git grep -FO'{_K} -f {_TARGET}' -e x -- {_FILE}",
        f"git grep -nEO'{_K} -f {_TARGET}' -e x -- {_FILE}",
        # -O after a number option, an upper-case program name, a mixed-case config key.
        f"git grep -3O'{_K} -f {_TARGET}' -e x -- {_FILE}",
        f"GIT grep -nO'{_K} -f {_TARGET}' -e x -- {_FILE}",
        f"git -c Core.Pager='{_K} -f {_TARGET}' grep -e x -- {_FILE}",
        # git run through a wrapper, with the pager on grep or in env's assignments.
        f"sudo git grep -nO'{_K} -f {_TARGET}' -e x -- {_FILE}",
        f"env GIT_PAGER='{_K} -f {_TARGET}' git grep -e x -- {_FILE}",
        # A git global option whose value is the next word, before grep.
        f"git -C . grep -nO'{_K} -f {_TARGET}' -e x -- {_FILE}",
        f"git -c user.name=x grep -O'{_K} -f {_TARGET}' -- {_FILE}",
        f"git --git-dir .git grep -O'{_K} -f {_TARGET}' -- {_FILE}",
        f"git --attr-source HEAD grep -nO'{_K} -f {_TARGET}' -e x -- {_FILE}",
        # -O still parsed as an option after a plain pathspec (no `--`).
        f"git grep -e x {_FILE} -O'{_K} -f {_TARGET}'",
    ],
)
def test_adjacent_pager_spellings_are_refused(cmd: str) -> None:
    assert _is_kill(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # ANSI-C quoting is decoded the way every floor reader decodes it.
        f"git grep $'-O{_K} -f {_TARGET}' x",
        f"git grep -e x $'-O{_K}\\x20-f\\x20{_TARGET}' -- {_FILE}",
        f"git grep -e x --open-files-in-pager=$'{_K} -f {_TARGET}' -- {_FILE}",
        # A wrapping shell in any letter case still hands its payload to the walk.
        f"BASH -c \"git grep -O'{_K} -f {_TARGET}' x\"",
        f"Sh -c \"git grep -O'{_K} -f {_TARGET}' x\"",
        # A quoted ``[*]`` expansion is ONE word, here the value of the -e before it.
        f"a=(x -e); git grep -F -e \"${{a[*]}}\" -O'{_K} -f {_TARGET} #' -e x -- {_FILE}",
        # An inserted element never takes the real option after it as its value.
        f"a=(x -e); git grep -F \"${{a[@]}}\" -O'{_K} -f {_TARGET} #' -e x -- {_FILE}",
        f"a=(x -e); git grep -F ${{a[@]}} -O'{_K} -f {_TARGET} #' -e x -- {_FILE}",
        # The last -O is the one git runs, and -P does not turn an explicit -O off.
        f"git grep -Ocat -O'{_K} -f {_TARGET} #' -e x -- src",
        f"git -P grep -O'{_K} -f {_TARGET} #' -e x -- src",
    ],
)
def test_quoting_wrappers_and_array_boundaries_do_not_hide_the_pager(cmd: str) -> None:
    assert _is_kill(cmd), cmd


# ---- must stay allowed: no pager, or the kill text is a pattern/other command ----


@pytest.mark.parametrize(
    "cmd",
    [
        # A plain git grep with no pager option.
        "git grep -n kirocrew -- src",
        "git grep -e foo -- '*.py'",
        f"git grep -n {_TARGET} -- {_FILE}",
        # -o / --only-matching and context flags are not a pager that kills.
        f"git grep -no {_TARGET} -- src",
        "git grep -C 3 -e kirocrew -- src",
        "git grep -Ovim kirocrew -- src",
        # A quoted separator in a pattern, with no pager.
        f"git grep -e 'x|y' -n -- {_FILE}",
        f"git grep -e 'x;y' -n -- {_FILE}",
        # An ambiguous long prefix (``--o``: ``--or`` or ``--open-files-in-pager``)
        # that git refuses, running no pager.
        f"git grep -e x --o='{_K} -f {_TARGET}' -- {_FILE}",
        # A global option after which git runs no grep at all.
        f"git --exec-path grep -O'{_K} -f {_TARGET}' -- src",
        f"git --version grep -O'{_K} -f {_TARGET}' -- src",
        # A wrapper with no pager, and a pager spelling that is only echoed.
        "sudo git grep -n kirocrew -- src",
        f"echo GIT_PAGER='{_K} -f {_TARGET}' git grep x",
        # A pager that is not a product kill.
        "GIT_PAGER=less git grep -O kirocrew -- src",
        f"git -c core.pager='{_K} -f {_TARGET}' log",
        # The kill text is an -e PATTERN, not a pager option.
        f"git grep -e '-O{_K} -f {_TARGET}' -- {_FILE}",
        f"git grep -e '{_K} -f {_TARGET}' -- {_FILE}",
        # In a cluster, everything after a value letter is that value, never a pager.
        f"git grep -eO'{_K} -f {_TARGET}' -- {_FILE}",
        f"git grep -nC3O'{_K} -f {_TARGET}' -e x -- {_FILE}",
        f"git grep --reg '-O{_K} -f {_TARGET}' -- {_FILE}",
        # POSIX environment names are case-sensitive: this is not git's pager.
        f"git_pager='{_K} -f {_TARGET}' git grep -e x -- {_FILE}",
        f"a=(-e '-O{_K} -f {_TARGET}'); git grep \"${{a[@]}}\" -- {_FILE}",
        # The kill text is a separate command git grep never reaches.
        f"git grep -e x; echo '-o{_K} -f {_TARGET}'",
        f"git log --grep '{_K} -f {_TARGET}'",
        # -P / --no-pager turns the configured pager off.
        f"git -P -c core.pager='{_K} -f {_TARGET}' grep -n {_TARGET} -- src",
        f"git --no-pager -c core.pager='{_K} -f {_TARGET}' grep -n {_TARGET} -- src",
        # A false pager.grep (any git boolean spelling) turns grep's paging off.
        f"git -c pager.grep=false grep -n {_TARGET} -- src",
        f"git -c pager.grep=false -c core.pager='{_K} -f {_TARGET}' grep -O {_TARGET} -- src",
        f"GIT_PAGER='{_K} -f {_TARGET}' git -c pager.grep=Off grep -n {_TARGET} -- src",
        # The last -O wins, so only ``cat`` runs.
        f"git grep -O'{_K} -f {_TARGET} #' -Ocat -e {_TARGET} -- src",
    ],
)
def test_a_git_grep_without_a_killing_pager_is_allowed(cmd: str) -> None:
    assert not _is_kill(cmd), _verdict(cmd)


# ---- cost: one pass per frame, linear in the number of words ---------------------


def test_a_long_pager_option_list_is_checked_in_linear_time(monkeypatch) -> None:
    # Work, not time: every argument the pager read parses goes through
    # ``_grep_arg_pager``, so the characters it is handed are the read's work. 4x the
    # words may cost at most 6x the work; a per-word rescan of the list costs 16x.
    real = argv_floor_pager._grep_arg_pager
    read = [0]

    def counting(word: str) -> "tuple[str, str]":
        read[0] += len(word)
        return real(word)

    monkeypatch.setattr(argv_floor_pager, "_grep_arg_pager", counting)

    def work(words: int) -> "tuple[int, int]":
        cmd = "git grep -e x " + f"-o{_K}x " * words + f" -- {_TARGET}"
        read[0] = 0
        security._is_self_kill(cmd.lower(), raw_text=cmd)
        return read[0], len(cmd)

    small, small_len = work(500)
    large, _ = work(2000)
    assert 0 < small <= 2 * small_len
    assert large <= 6 * small, (small, large)


@pytest.mark.parametrize(
    "build",
    [
        # One long array element, referenced many times.
        lambda size: "a=(-O'"
        + "x " * (size // 8)
        + "'); git grep "
        + '"${a[@]}" ' * (size // 12)
        + "foo",
        # Many short elements, referenced as many times.
        lambda size: "a=("
        + "-Ox " * (size // 14)
        + "); git grep "
        + '"${a[@]}" ' * (size // 14)
        + "foo",
    ],
)
def test_repeated_array_references_are_read_once(monkeypatch, build) -> None:
    # Work, not time, up to 36 KB: an array reference repeated k times must not cost
    # k times the array. 4x the command may cost at most 6x the work.
    real = argv_floor_pager._grep_arg_pager
    read = [0]

    def counting(word: str) -> "tuple[str, str]":
        read[0] += len(word)
        return real(word)

    monkeypatch.setattr(argv_floor_pager, "_grep_arg_pager", counting)

    def work(size: int) -> "tuple[int, int]":
        cmd = build(size)
        read[0] = 0
        security._is_self_kill(cmd.lower(), raw_text=cmd)
        return read[0], len(cmd)

    small, small_len = work(9_000)
    large, large_len = work(36_000)
    assert large_len >= 36_000
    assert 0 < small <= 2 * small_len
    assert large <= 6 * small, (small, large)


# ---- the command as submitted feeds only the pager read --------------------------
# The gate hands the kill check the command with its case, for git's case-sensitive
# options. Every other kill shape still reads lower-cased text, so its verdict does
# not depend on how the command is capitalised.


@pytest.mark.parametrize(
    "cmd",
    [
        f"{_K.upper()} -f {_TARGET}",
        f"Pkill -f {_TARGET.title()}",
        f"sudo {_K.upper()} -f {_TARGET}",
        f"BASH -c '{_K} -f {_TARGET}'",
        "KILL -9 $(pgrep -f kirocrew)",
        "Kill $(pidof KIROCREW)",
    ],
)
def test_a_capitalised_kill_is_still_refused(cmd: str) -> None:
    assert _is_kill(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    ["Kill $PPID", "kill 1234", f"{_K.upper()} -f Firefox", f"echo {_K.upper()} -f {_TARGET}"],
)
def test_a_capitalised_command_that_kills_nothing_of_ours_is_still_allowed(cmd: str) -> None:
    assert not _is_kill(cmd), _verdict(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        f"{_K.upper()} -f {_TARGET}",
        "Kill $(pidof KIROCREW)",
        "Kill $PPID",
        f"echo {_K.upper()} -f {_TARGET}",
    ],
)
def test_the_submitted_case_changes_no_verdict_outside_a_git_grep(cmd: str) -> None:
    lower = cmd.lower()
    assert security._is_self_kill(lower, raw_text=cmd) == security._is_self_kill(lower)


@pytest.mark.parametrize(
    "cmd",
    [
        "BASH -c 'chmod -R 777 /'",
        "echo 'chmod -R 777 /' | Sh",
        "Sh <<< 'chmod -R 777 /'",
        "BASH -c 'echo hi'",
    ],
)
def test_the_shell_name_fold_reaches_only_the_self_kill_floor(cmd: str, monkeypatch) -> None:
    # Main gap, out of scope here: a mixed-case wrapper shell bypasses non-kill deny rules.
    # The pager read folds a wrapping shell's name; with that fold switched off, no
    # verdict on a command that kills nothing may change.
    with_fold = bool(security.is_denied(cmd))
    walk = shell_normalizer._shell_payload_walk
    monkeypatch.setattr(argv_floor_self, "_shell_payload_walk", lambda text, **_kw: walk(text))
    assert bool(security.is_denied(cmd)) == with_fold


# ---- what the same simple command shows ------------------------------------------
# A here-document fed to a shell, and every pager source git reads that the git
# command itself carries: config set by environment words before ``git``,
# ``--config-env`` naming such a word, and an alias set by ``-c``.


@pytest.mark.parametrize(
    "cmd",
    [
        f"bash <<'EOF'\ngit grep -O'{_K} -f {_TARGET}' x\nEOF",
        f"bash -s <<EOF\ngit grep -O'{_K} -f {_TARGET}' x\nEOF",
        f"git grep --no-open-files-in-pager -O'{_K} -f {_TARGET}' x",
        f"git -c pager.grep=true -c pager.grep='{_K} -f {_TARGET}' grep -n x -- src",
        f"git -c core.pager=cat -c core.pager='{_K} -f {_TARGET}' grep -n x -- src",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.pager"
        f" GIT_CONFIG_VALUE_0='{_K} -f {_TARGET}' git grep -O x",
        f"GIT_CONFIG_PARAMETERS=\"'core.pager'='{_K} -f {_TARGET}'\" git grep -O x",
        f"X='{_K} -f {_TARGET}' git --config-env=core.pager=X grep -O x",
        f"X='{_K} -f {_TARGET}' git --config-env pager.grep=X grep x",
        f"git -c alias.g=grep g -O'{_K} -f {_TARGET}' x",
        f"git -c alias.g=\"grep -O'{_K} -f {_TARGET}'\" g x",
        f"git -c alias.h=\"grep -O'{_K} -f {_TARGET}'\" -c alias.g=h g x",
        f"git -p -c pager.grep=false -c core.pager='{_K} -f {_TARGET}' grep x",
        f"bash -o pipefail <<EOF\ngit grep -O'{_K} -f {_TARGET}' x\nEOF",
        f"bash --norc <<EOF\ngit grep -O'{_K} -f {_TARGET}' x\nEOF",
        f"bash <<A\nbash <<B\ngit grep -O'{_K} -f {_TARGET}' x\nB\nA",
        f"find . -exec git grep -O'{_K} -f {_TARGET}' x {{}} +",
    ],
)
def test_a_pager_the_same_command_shows_is_refused(cmd: str) -> None:
    assert _is_kill(cmd), _verdict(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "bash <<'EOF'\ngit grep -Ocat x\nEOF",
        f"cat <<'EOF'\ngit grep -O'{_K} -f {_TARGET}' x\nEOF",
        f"git grep -O'{_K} -f {_TARGET}' --no-open-files-in-pager x",
        f"git -c pager.grep='{_K} -f {_TARGET}' -c pager.grep=true grep -n x -- src",
        f"git -c core.pager='{_K} -f {_TARGET}' -c core.pager=cat grep -n x -- src",
        "GIT_CONFIG_COUNT=0 GIT_CONFIG_KEY_0=core.pager"
        f" GIT_CONFIG_VALUE_0='{_K} -f {_TARGET}' git grep -O x",
        f"GIT_CONFIG_PARAMETERS=\"'user.name'='{_K} -f {_TARGET}'\" git grep -O x",
        "X=less git --config-env=core.pager=X grep -O x",
        "git --config-env=core.pager=X grep -O x",
        "git -c alias.g=grep g -Ocat x",
        f"git -c alias.g=log g -O'{_K} -f {_TARGET}' x",
        f"git -c alias.g=h -c alias.h=g g -O'{_K} -f {_TARGET}' x",
        f"git -p -c pager.grep='{_K} -f {_TARGET}' grep x",
        f"bash -ec 'echo hi' <<EOF\ngit grep -O'{_K} -f {_TARGET}' x\nEOF",
        f"bash script.sh <<EOF\ngit grep -O'{_K} -f {_TARGET}' x\nEOF",
        "bash <<A\nbash <<B\ngit grep -Ocat x\nB\nA",
        "find . -exec git grep -Ocat x {} +",
    ],
)
def test_a_pager_the_same_command_overrides_or_never_runs_is_allowed(cmd: str) -> None:
    assert not _is_kill(cmd), _verdict(cmd)


# ---- arrays as bash runs the text's assignments ----------------------------------
# The floor's own pass: the last assignment wins, ``+=`` appends, ``a[N]=`` sets one
# index, and ``$a`` / ``${a[0]}`` / ``${a[N]}`` read one element.


@pytest.mark.parametrize(
    "cmd",
    [
        f"a=(-Ocat); a=(-O'{_K} -f {_TARGET}'); git grep \"${{a[@]}}\" x",
        f"a=(-n); a+=(-O'{_K} -f {_TARGET}'); git grep \"${{a[@]}}\" x",
        f"a=(-n -i); a[1]=-O'{_K} -f {_TARGET}'; git grep \"${{a[@]}}\" x",
        f"a=(-O'{_K} -f {_TARGET}'); git grep \"$a\" x",
        f"a=(-O'{_K} -f {_TARGET}'); git grep \"${{a[0]}}\" x",
        f"a=(-n -O'{_K} -f {_TARGET}'); git grep \"${{a[1]}}\" x",
    ],
)
def test_an_array_pager_is_read_as_bash_assigns_it(cmd: str) -> None:
    assert _is_kill(cmd), _verdict(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        f"a=(-O'{_K} -f {_TARGET}'); a=(-Ocat); git grep \"${{a[@]}}\" x",
        'a=(-n); a+=(-Ocat); git grep "${a[@]}" x',
        f"a=(-O'{_K} -f {_TARGET}'); a[0]=-Ocat; git grep \"${{a[@]}}\" x",
        'a=(-Ocat); git grep "$a" x',
        f"a=(-n -O'{_K} -f {_TARGET}'); git grep \"${{a[0]}}\" x",
        f"a=(-O'{_K} -f {_TARGET}' -n); git grep \"${{a[1]}}\" x",
    ],
)
def test_an_array_element_bash_does_not_pass_is_not_read(cmd: str) -> None:
    assert not _is_kill(cmd), _verdict(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        f"a=([0]='-O{_K} -f {_TARGET}'); git grep \"${{a[@]}}\" x",
        f"a=(-n [5]='-O{_K} -f {_TARGET}'); git grep \"${{a[5]}}\" x",
        # bash reads a leading-zero subscript as octal: a[010] is index 8.
        f"a=(); a[010]='-O{_K} -f {_TARGET}'; git grep \"${{a[8]}}\" x",
        f"bash -- <<'EOF'\ngit grep -O'{_K} -f {_TARGET}' x\nEOF",
        f"git -c core.pager='{_K} -f {_TARGET}' grep -O x -- src",
    ],
)
def test_compound_and_octal_subscripts_and_bare_pagers_are_refused(cmd: str) -> None:
    assert _is_kill(cmd), _verdict(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "a=([0]='-Ocat'); git grep \"${a[@]}\" x",
        f"a=(); a[010]='-O{_K} -f {_TARGET}'; git grep \"${{a[10]}}\" x",
        # -O<cmd> names the pager, so core.pager / GIT_PAGER do not run.
        f"git -c core.pager='{_K} -f {_TARGET}' grep -Ocat x -- src",
        f"GIT_PAGER='{_K} -f {_TARGET}' git grep -Ocat x -- src",
        # With a script operand the shell runs the file, and the heredoc is its input.
        f"bash -- /dev/null <<'EOF'\ngit grep -O'{_K} -f {_TARGET}' x\nEOF",
    ],
)
def test_a_pager_git_never_runs_is_allowed(cmd: str) -> None:
    assert not _is_kill(cmd), _verdict(cmd)


def test_an_expansion_past_the_budget_is_refused_without_building_it(monkeypatch) -> None:
    # One 17 KB command that repeats a 5 KB element 1,700 times inside one -O word:
    # sized before anything is built, it is refused with no word read at all.
    real = argv_floor_pager._grep_arg_pager
    read = [0]

    def counting(word: str) -> "tuple[str, str]":
        read[0] += len(word)
        return real(word)

    monkeypatch.setattr(argv_floor_pager, "_grep_arg_pager", counting)
    cmd = "a=('" + f"{_TARGET} " * 600 + "'); git grep \"-O" + "${a[0]}" * 1700 + '" x'
    assert len(cmd) > 17_000
    assert security._is_self_kill(cmd.lower(), raw_text=cmd)
    assert read[0] == 0
    # An ordinary array and pager stay well inside the budget.
    read[0] = 0
    small = "a=(-n -i -w -e x); git grep \"${a[@]}\" -O'cat' -- src"
    assert not security._is_self_kill(small.lower(), raw_text=small)
    assert 0 < read[0] <= 2 * len(small)


# ---- long numbers, quoted names, scalars, conditionals, git's own parsing ----------
_LONG = "1" + "0" * 4999
_PADDED = "0" * 4301 + "1"


@pytest.mark.parametrize(
    "cmd",
    [
        f"GIT_CONFIG_COUNT={_LONG} GIT_CONFIG_KEY_0=core.pager GIT_CONFIG_VALUE_0=cat git grep x {_TARGET}",
        f"GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_{_LONG}=core.pager GIT_CONFIG_VALUE_{_LONG}=cat git grep x",
        f"a=(x); a[{_LONG}]=y; git grep x {_TARGET}",
        f'a=(x); git grep "${{a[{_LONG}]}}" {_TARGET}',
        f'a=([{_LONG}]=y); git grep "${{a[@]}}" {_TARGET}',
        f"GIT_CONFIG_COUNT={_PADDED} GIT_CONFIG_KEY_0=core.pager GIT_CONFIG_VALUE_0=cat git grep x {_TARGET}",
    ],
)
def test_a_5000_digit_number_never_raises_out_of_the_gate(cmd: str) -> None:
    # Every parser entry that converts a number: GIT_CONFIG_COUNT, a GIT_CONFIG_KEY_<n>
    # name, an assigned subscript, a read subscript and a compound subscript.
    security.is_denied(cmd)
    security._is_self_kill(cmd.lower(), raw_text=cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        # A number too long to read, on the pager path, is refused.
        f"a=(x); a[{_LONG}]=y; git grep x {_TARGET}",
        # The program name with quoting bash removes.
        f"g''it grep -O'{_K} -f {_TARGET}' x",
        f"gi\\t grep -O'{_K} -f {_TARGET}' x",
        # A scalar set in the same chain.
        f"p='-O{_K} -f {_TARGET}'; git grep \"$p\" x",
        f"p='{_K} -f {_TARGET}'; git grep -O\"$p\" x",
        # A conditional assignment does not erase an earlier killing value.
        f"a=('-O{_K} -f {_TARGET} #'); false && a=(-Ocat); git grep \"${{a[@]}}\" -e x -- src",
        # git reads GIT_CONFIG_COUNT=+1 as one, and a zero-padded count by its value.
        "GIT_CONFIG_COUNT=+1 GIT_CONFIG_KEY_0=core.pager"
        f" GIT_CONFIG_VALUE_0='{_K} -f {_TARGET}' git grep -O -e x -- src",
        f"GIT_CONFIG_COUNT={_PADDED} GIT_CONFIG_KEY_0=core.pager"
        f" GIT_CONFIG_VALUE_0='{_K} -f {_TARGET}' git grep -O -e x -- src",
        # GIT_PAGER is the pager git runs.
        f"GIT_PAGER='{_K} -f {_TARGET}' git -c core.pager=cat grep -O -e x -- src",
        # An alias whose words start with git's own -p.
        f"git -c alias.g='-p grep' -c core.pager='{_K} -f {_TARGET}' g x",
    ],
)
def test_a_pager_bash_and_git_would_still_run_is_refused(cmd: str) -> None:
    assert _is_kill(cmd), _verdict(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        # A long number off the pager path is not a refusal.
        f"a=(x); a[{_LONG}]=y; echo {_TARGET}",
        "g''it grep -Ocat x",
        'p=-Ocat; git grep "$p" x',
        'a=(-Ocat); false && a=(-n); git grep "${a[@]}" x',
        "GIT_CONFIG_COUNT=-1 GIT_CONFIG_KEY_0=core.pager"
        f" GIT_CONFIG_VALUE_0='{_K} -f {_TARGET}' git grep -O -e x -- src",
        f"GIT_PAGER=cat git -c core.pager='{_K} -f {_TARGET}' grep -O -e x -- src",
        f"git -c alias.g='-P grep' -c core.pager='{_K} -f {_TARGET}' g x",
        # An honest script that grows an array and greps after each step.
        "a=(); "
        + "".join(
            f'a+=(-e pattern_number_{k}); git grep "${{a[@]}}" -- src/kiro_crew; '
            for k in range(50)
        ),
    ],
)
def test_a_command_bash_and_git_run_without_a_killing_pager_is_allowed(cmd: str) -> None:
    assert not _is_kill(cmd), _verdict(cmd[:200])


# ---- the candidate caps: only a value that could reach a pager counts -------------

_LINT = (
    "rc=0; ruff check src || rc=1; ruff format --check src || rc=1; mypy src || rc=1; "
    "pytest -q test/unit || rc=1; pytest -q test/integration || rc=1; "
    "bandit -q -r src || rc=1; pip-audit || rc=1; codespell src || rc=1; "
    "shellcheck scripts/*.sh || rc=1; git grep -n 'TODO(kirocrew)' -- src && rc=1; exit $rc"
)
_FLAGS = (
    'i=; w=; n=; l=; c=; [ -n "$A" ] && i=-i; [ -n "$B" ] && w=-w; [ -n "$C" ] && n=-n; '
    '[ -n "$D" ] && l=-l; [ -n "$E" ] && c=-c; git grep $i $w $n $l $c -e kirocrew -- src'
)


@pytest.mark.parametrize(
    "cmd",
    [
        _LINT,
        _FLAGS,
        # Past 16 combinations, a search pattern that names a pager setting.
        _FLAGS.replace("-e kirocrew -- src", "-e pager -- src/kiro_crew"),
        _FLAGS.replace("-e kirocrew -- src", "-e GIT_CONFIG_COUNT -- src/kiro_crew"),
        # Past 16 combinations: long options that start --o but are not the pager,
        # and a pattern value holding a pager setting (Security Scope Review rows).
        _FLAGS.replace("-e kirocrew", "--only-matching -e kirocrew"),
        _FLAGS.replace("-e kirocrew", "-e kirocrew --or -e KIROCREW_HOME"),
        _FLAGS.replace("-e kirocrew -- src", "-e 'core.pager=' -- src/kiro_crew docs"),
        _FLAGS.replace("-e kirocrew -- src", "--regexp GIT_PAGER= -- src"),
        # A single-quoted regex naming a pager setting is passed as written, so it
        # is a pattern too (Security Scope Review row on a5f6d2ba16).
        'i=; n=; w=; l=; c=; [ -n "$IC" ] && i=-i; [ -n "$LN" ] && n=-n; '
        '[ -n "$WD" ] && w=-w; [ -n "$FL" ] && l=-l; [ -n "$CT" ] && c=-c; '
        "git grep $i $n $w $l $c -E 'core\\.pager\\s*=' -- src/kiro_crew",
        # A variable past the cap that the grep never reads.
        "".join(f"pytest test_{i}.py && status=OK\n" for i in range(10))
        + 'echo "$status"\ngit grep -n TODO src',
        "a=(-Ocat); " + "false && a=(-n); " * 9 + "git grep -n kirocrew -- src",
    ],
)
def test_an_ordinary_script_past_the_candidate_caps_is_allowed(cmd: str) -> None:
    assert security.is_denied(cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        # Past 8 candidates of one array, one of them a killing -O.
        f"a=('-O{_K} -f {_TARGET} #'); "
        + "".join(f"false && a=(-n{i}); " for i in range(9))
        + 'git grep "${a[@]}" -e x -- src',
        # Past 16 combinations of inert flags, with a killing -O in the same grep.
        _FLAGS.replace("-e kirocrew", f"-O'{_K} -f {_TARGET} #' -e x"),
        # The same with the long option and a unique prefix of it.
        _FLAGS.replace("-e kirocrew", f"--open-files-in-pager='{_K} -f {_TARGET} #' -e x"),
        _FLAGS.replace("-e kirocrew", f"--op='{_K} -f {_TARGET} #' -e x"),
        # A variable that may end in -e, so the literal -e after it may become a
        # pattern and the -O word a real pager.
        _FLAGS.replace("git grep $i", '[ -n "$F" ] && a=-e; git grep $a $i').replace(
            "-e kirocrew", f"-e '-O{_K} -f {_TARGET} #'"
        ),
        # A killing -O after a value option's own value is still read.
        _FLAGS.replace("-e kirocrew", f"-e x -O'{_K} -f {_TARGET} #'"),
        # A killing -O quoted whole, next to a quoted regex naming a pager setting.
        _FLAGS.replace("-e kirocrew", f"'-O{_K} -f {_TARGET} #' -E 'core\\.pager\\s*='"),
    ],
)
def test_a_killing_pager_past_the_candidate_caps_is_refused(cmd: str) -> None:
    assert _is_kill(cmd)


def test_the_cap_reads_only_long_options_that_can_become_the_pager() -> None:
    pager = argv_floor_pager

    for word in ("--o", "--op", "--open=x", "--open-files-in-pager", "--o$p", '--o"$p"'):
        assert pager._may_reach_pager(word), word
    for word in ("--or", "--only-matching", "--only", "--max-count=3"):
        assert not pager._may_reach_pager(word), word


def test_the_cap_reads_grep_words_for_pager_options_only() -> None:
    pager = argv_floor_pager

    def words(*values: str) -> "list[tuple[str, list[int]]]":
        return [(value, []) for value in values]

    # A setting spelled among grep's own words is a pattern or a path there.
    assert not pager._grep_words_may_reach_pager(words("-e", "core.pager=", "--", "src"))
    assert not pager._grep_words_may_reach_pager(words("--regexp", "GIT_PAGER="))
    # An -O is read wherever it stands: an expansion before it may shift which
    # word is a value option's value (``a=-e; git grep $a -e '-O<kill>'``).
    assert pager._grep_words_may_reach_pager(words("$a", "-e", "-Ox"))
    assert pager._grep_words_may_reach_pager(words("-e", "x", "-Ocat"))
    # Brace expansion and command substitution still expand into a pager option.
    assert pager._grep_words_may_reach_pager(words("--o{p=x,r}"))
    assert pager._grep_words_may_reach_pager(words("-e", "`echo x -Ox`"))
    # A substitution inside double quotes reaches here as the reader's placeholder.
    assert pager._grep_words_may_reach_pager(words("--o\x01=x"))
    assert pager._grep_words_may_reach_pager(words("--{open-files-in-pager=x,y}"))
    assert pager._grep_words_may_reach_pager(words("${u:---open-files-in-pager=x}"))
    assert pager._grep_words_may_reach_pager(words("--o@(p)=x"))
    # Quoted literally, a regex naming a setting is a pattern; unquoted, ``\`` and
    # ``*`` may still expand, so it is not.
    regex = "core\\.pager\\s*="
    assert not pager._grep_words_may_reach_pager([(regex, [1] * len(regex))])
    assert pager._grep_words_may_reach_pager([(regex, [0] * len(regex))])
