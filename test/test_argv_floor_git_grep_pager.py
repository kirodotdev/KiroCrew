"""The self-kill floor's ``git grep`` pager read: the pager a grep names on its line.

``git grep`` runs a pager over the matching files, taken from ``-O`` /
``--open-files-in-pager``, a ``-c core.pager=`` / ``-c pager.grep=`` global
option, or ``GIT_PAGER`` / ``PAGER`` in the environment. The self-kill argv
floor reads ``pkill`` / ``killall`` only as a word of the command, so a kill
handed to git as a pager has to be read out of that option to be seen. The floor
refuses a ``git grep`` whose pager names a product kill within the same submitted
command line (a clustered ``-O``, a ``-c core.pager=`` / ``pager.grep=``, a
``GIT_PAGER=`` / ``PAGER=`` before ``git``, or a literal array that carries one).
A word after ``--`` is a path to git, so a ``-O`` there runs no pager and the
command is allowed.

The pager is read the way git parses it, one invocation at a time and with the
case as submitted: option parsing stops at ``--``, at ``--end-of-options`` and
at the first non-option word (``(`` and ``)`` are options to git), a value
option's value is never a pager, the last ``-O`` wins, a form where git starts no
pager (``-q`` without ``-O``, a zero max count, ``--cached`` with ``-O``, an option
after the first non-option word) is not refused, and a
value this read cannot see on the same line (an environment variable set by an
earlier statement, a non-literal array, a config file, an alias, a
here-document, ``find -exec``) is not read, so the command keeps its verdict.
"""

from __future__ import annotations

import pytest

from kiro_crew import security
from kiro_crew.security import argv_rules

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


# ---- forms 2 to 5: refused when the pager kills the product ----------------------


@pytest.mark.parametrize(
    "cmd",
    [
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
def test_forms_2_to_5_are_refused(cmd: str) -> None:
    assert _is_kill(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # 1) -O placed after the pathspec: git reads it as a path, so no pager runs.
        f"git grep -e x -- {_FILE} -O'{_K} -f {_TARGET} #'",
        # The same -O misplaced after a bare pathspec (git errors; no pager runs).
        f"git grep -e x {_FILE} -O'{_K} -f {_TARGET}'",
    ],
)
def test_form_1_stays_allowed(cmd: str) -> None:
    assert not _is_kill(cmd), _verdict(cmd)


@pytest.mark.parametrize("sep", [";", "|"])
@pytest.mark.parametrize(
    "form",
    [
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
        # The last -O wins, and -P does not turn an explicit -O off.
        f"git grep -Ocat -O'{_K} -f {_TARGET} #' -e x -- src",
        f"git -P grep -O'{_K} -f {_TARGET} #' -e x -- src",
        # The last core.pager / pager.grep git reads wins.
        f"git -c core.pager=cat -c core.pager='{_K} -f {_TARGET}' grep -n x -- src",
        f"git -c pager.grep=true -c pager.grep='{_K} -f {_TARGET}' grep -n x -- src",
        # A --no-open-files-in-pager before -O does not clear the later -O.
        f"git grep --no-open-files-in-pager -O'{_K} -f {_TARGET}' x",
        # ``(`` and ``)`` are grep options with no dash, so git parses on past them.
        f"git grep '(' -e x ')' -O'{_K} -f {_TARGET}'",
        f"git grep -e x '(' -O'{_K} -f {_TARGET}' -e y ')'",
        f"a=('(' -e x ')'); git grep \"${{a[@]}}\" -O'{_K} -f {_TARGET}'",
        # An -O before --end-of-options is still read.
        f"git grep -O'{_K} -f {_TARGET}' --end-of-options x",
        # git reads a number in pager.grep as a boolean (non-zero is true), so
        # core.pager / GIT_PAGER runs.
        f"git -c pager.grep=10 -c core.pager='{_K} -f {_TARGET}' grep x",
        f"git -c pager.grep=0x1 -c core.pager='{_K} -f {_TARGET}' grep x",
        f"git -c pager.grep=2k -c core.pager='{_K} -f {_TARGET}' grep x",
        f"GIT_PAGER='{_K} -f {_TARGET}' git -c pager.grep=' 1' grep x",
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
    ],
)
def test_ansi_c_quoting_in_the_pager_is_decoded(cmd: str) -> None:
    assert _is_kill(cmd), cmd


# ---- arrays as bash runs the text's literal assignments --------------------------
# The floor's own pass: the last assignment wins, ``+=`` appends, a plain scalar is
# element 0, and ``$a`` / ``${a[0]}`` / ``${a[N]}`` read one element.


@pytest.mark.parametrize(
    "cmd",
    [
        f"a=(-Ocat); a=(-O'{_K} -f {_TARGET}'); git grep \"${{a[@]}}\" x",
        f"a=(-n); a+=(-O'{_K} -f {_TARGET}'); git grep \"${{a[@]}}\" x",
        f"a=(-O'{_K} -f {_TARGET}'); git grep \"$a\" x",
        f"a=(-O'{_K} -f {_TARGET}'); git grep \"${{a[0]}}\" x",
        f"a=(-n -O'{_K} -f {_TARGET}'); git grep \"${{a[1]}}\" x",
        # bash reads a subscript with a leading zero as octal: 01 is element 1.
        f"a=(-n -O'{_K} -f {_TARGET}'); git grep \"${{a[01]}}\" x",
        # A plain scalar set in the same chain is read as element 0.
        f"p='-O{_K} -f {_TARGET}'; git grep \"$p\" x",
        f"p='{_K} -f {_TARGET}'; git grep -O\"$p\" x",
    ],
)
def test_an_array_pager_is_read_as_bash_assigns_it(cmd: str) -> None:
    assert _is_kill(cmd), _verdict(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        # The last whole assignment wins, so only ``cat`` runs.
        f"a=(-O'{_K} -f {_TARGET}'); a=(-Ocat); git grep \"${{a[@]}}\" x",
        'a=(-n); a+=(-Ocat); git grep "${a[@]}" x',
        'a=(-Ocat); git grep "$a" x',
        # A subscript write this read does not model marks the array unreadable, so
        # a stale killing value is not read: the command keeps its verdict.
        f"a=(-O'{_K} -f {_TARGET}'); a[0]=-Ocat; git grep \"${{a[@]}}\" x",
    ],
)
def test_an_array_whose_killing_value_is_replaced_or_unreadable_is_allowed(cmd: str) -> None:
    assert not _is_kill(cmd), _verdict(cmd)


# ---- must stay allowed: no pager, or the kill text is a pattern / other command --


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
        # ``-O<cmd>`` names the pager, so core.pager / GIT_PAGER do not run beside it.
        f"git -c core.pager='{_K} -f {_TARGET}' grep -Ocat x -- src",
        f"GIT_PAGER='{_K} -f {_TARGET}' git grep -Ocat x -- src",
        # The last core.pager git reads wins.
        f"git -c core.pager='{_K} -f {_TARGET}' -c core.pager=cat grep -n x -- src",
        f"git -c pager.grep='{_K} -f {_TARGET}' -c pager.grep=true grep -n x -- src",
        # A real --no-open-files-in-pager after the -O clears it.
        f"git grep -O'{_K} -f {_TARGET}' --no-open-files-in-pager x",
        # A lone ``-`` and ``--end-of-options`` end git's option parsing.
        f"git grep - -O'{_K} -f {_TARGET}'",
        f"git grep --end-of-options -O'{_K} -f {_TARGET}' x",
        # A zero number in pager.grep is false; a value git cannot read as a boolean
        # (`` true``) is itself the pager command, so core.pager does not run.
        f"git -c pager.grep=00 -c core.pager='{_K} -f {_TARGET}' grep x",
        f"git -c pager.grep=' true' -c core.pager='{_K} -f {_TARGET}' grep x",
        # ``unset`` removes the value, so ``"$p"`` expands to nothing.
        f"p='-O{_K} -f {_TARGET}'; unset p; git grep \"$p\" x",
    ],
)
def test_a_git_grep_without_a_killing_pager_is_allowed(cmd: str) -> None:
    assert not _is_kill(cmd), _verdict(cmd)


_PG = f"'{_K} -f {_TARGET}'"


@pytest.mark.parametrize(
    "cmd",
    [
        # -q, or a max count git reads as 0, and grep starts no pager of its own.
        f"GIT_PAGER={_PG} git grep -q x",
        f"GIT_PAGER={_PG} git grep --quiet x",
        f"GIT_PAGER={_PG} git grep --qu x",
        f"GIT_PAGER={_PG} git grep -iq x",
        f"GIT_PAGER={_PG} git grep -qe x",
        f"GIT_PAGER={_PG} git grep -m0 x",
        f"GIT_PAGER={_PG} git grep -m 0 x",
        f"GIT_PAGER={_PG} git grep -m00 x",
        f"GIT_PAGER={_PG} git grep '-m 0' x",
        f"GIT_PAGER={_PG} git grep -m0x0 x",
        f"GIT_PAGER={_PG} git grep -m0k x",
        f"GIT_PAGER={_PG} git grep -m1 -m0 x",
        f"GIT_PAGER={_PG} git grep --max-count=0 x",
        f"GIT_PAGER={_PG} git grep --max-count 0 x",
        f"GIT_PAGER={_PG} git grep --no-max-count x",
        f"git -c core.pager={_PG} grep -q x",
        f"git -c core.pager={_PG} grep -m0 x",
        f"PAGER={_PG} git grep -q x",
        # -O: a zero max count, or --cached (git dies), and grep runs no pager.
        f"git grep -m0 -O{_PG} x",
        f"git grep --max-count 0 -O{_PG} x",
        f"git grep -O{_PG} -m0 x",
        f"git grep --cached -O{_PG} x",
        f"git grep -O{_PG} --cached x",
        f"git grep --ca -O{_PG} x",
        f"GIT_PAGER={_PG} git grep -O -m0 x",
        # An option after the first non-option word: git dies before any pager.
        f"GIT_PAGER={_PG} git grep x -q",
        f"GIT_PAGER={_PG} git grep -e x y -c",
        f"git grep -O{_PG} x -m0",
        f"git grep -e x y -O{_PG}",
        f"GIT_PAGER={_PG} git grep --end-of-options x -c",
    ],
)
def test_a_git_grep_that_starts_no_pager_is_allowed(cmd: str) -> None:
    assert not _is_kill(cmd), _verdict(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        # -q does not stop -O, nor a bare -O's default pager.
        f"git grep -q -O{_PG} x",
        f"git grep -O{_PG} -q x",
        f"git grep -qO{_PG} x",
        f"GIT_PAGER={_PG} git grep -O -q x",
        # -c / -l still print, so grep pages.
        f"GIT_PAGER={_PG} git grep -c x",
        f"GIT_PAGER={_PG} git grep -l x",
        # A non-zero max count, the last count, and a cleared -q or --cached.
        f"GIT_PAGER={_PG} git grep -m1 x",
        f"GIT_PAGER={_PG} git grep -m0 -m1 x",
        f"GIT_PAGER={_PG} git grep --no-max-count -m1 x",
        f"GIT_PAGER={_PG} git grep -q --no-quiet x",
        f"git grep --cached --no-cached -O{_PG} x",
        # -e takes the next word, so it is a pattern, not -q / -m0.
        f"GIT_PAGER={_PG} git grep -e x -e -q",
        f"GIT_PAGER={_PG} git grep -e x -- -q",
        # -q after -e or --end-of-options is a pattern: grep pages a matching path x.
        f"GIT_PAGER={_PG} git grep -e -q x",
        f"GIT_PAGER={_PG} git grep --end-of-options -q x",
        # git starts the pager itself under -p or a true / command pager.grep.
        f"GIT_PAGER={_PG} git -p grep -q x",
        f"GIT_PAGER={_PG} git -p grep -m0 -Ocat x",
        f"GIT_PAGER={_PG} git -c pager.grep=true grep -q x",
        f"git -c pager.grep={_PG} grep x -q",
        # ``unset -f`` removes a function, not the variable.
        f"p='-O{_K} -f {_TARGET}'; unset -f p; git grep \"$p\" x",
    ],
)
def test_a_git_grep_that_still_runs_its_pager_is_refused(cmd: str) -> None:
    assert _is_kill(cmd), cmd


def test_a_subscript_too_long_to_convert_keeps_mains_verdict() -> None:
    # More digits than int() converts: left unread, never raised on.
    assert _verdict('a=(x); git grep -e x "${a[' + "9" * 5000 + ']}"') is None


# ---- out of scope here: a pager source this read does not follow (residuals that
# main also allows): a config file, an alias, a here-document, find -exec, and the
# GIT_CONFIG_* / --config-env environment pagers.


@pytest.mark.parametrize(
    "cmd",
    [
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.pager"
        f" GIT_CONFIG_VALUE_0='{_K} -f {_TARGET}' git grep -O x",
        f"GIT_CONFIG_PARAMETERS=\"'core.pager'='{_K} -f {_TARGET}'\" git grep -O x",
        f"X='{_K} -f {_TARGET}' git --config-env=core.pager=X grep -O x",
        f"git -c alias.g=grep g -O'{_K} -f {_TARGET}' x",
        f"git -c alias.g=\"grep -O'{_K} -f {_TARGET}'\" g x",
        f"bash <<'EOF'\ngit grep -O'{_K} -f {_TARGET}' x\nEOF",
        f"bash -s <<EOF\ngit grep -O'{_K} -f {_TARGET}' x\nEOF",
        f"find . -exec git grep -O'{_K} -f {_TARGET}' x {{}} +",
    ],
)
def test_a_pager_source_out_of_this_scope_keeps_mains_verdict(cmd: str) -> None:
    # main does not read any of these either; they are named residuals, not refusals.
    assert not _is_kill(cmd), _verdict(cmd)


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
        "KILL -9 $(pgrep -f kirocrew)",
        "Kill $(pidof KIROCREW)",
    ],
)
def test_a_capitalised_kill_is_still_refused(cmd: str) -> None:
    assert _is_kill(cmd), cmd


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


# ---- cost: one pass over the options, expansions bounded by the text -------------


def test_the_pager_option_scan_reads_each_word_once(monkeypatch) -> None:
    # Work, not time: every argument the pager read parses goes through
    # ``_grep_arg_pager``, so the characters it is handed are the read's work. 4x the
    # words may cost at most 6x the work; a per-word rescan of the list costs 16x.
    real = argv_rules._grep_arg_pager
    read = [0]

    def counting(word: str) -> "tuple[str, str, bool]":
        read[0] += len(word)
        return real(word)

    monkeypatch.setattr(argv_rules, "_grep_arg_pager", counting)

    def work(words: int) -> "tuple[int, int]":
        cmd = "git grep " + f"-o{_K}x " * words + f"-e x -- {_TARGET}"
        read[0] = 0
        security._is_self_kill(cmd.lower(), raw_text=cmd)
        return read[0], len(cmd)

    small, small_len = work(500)
    large, _ = work(2000)
    assert 0 < small <= 2 * small_len
    assert large <= 6 * small, (small, large)


@pytest.mark.parametrize("ref", ['"${a[*]}"', '"${a[@]}"', '"$a"'])
def test_array_expansion_work_stays_within_the_text_length(monkeypatch, ref: str) -> None:
    # Work, not time: the characters the expansions produce. A word that repeats an
    # array reference n times would build n copies of the array; the read stops
    # once they pass the text's own length and leaves that invocation unread.
    real = argv_rules._expand_word
    made = [0]

    def counting(*args: object) -> "list[str] | None":
        words = real(*args)  # type: ignore[arg-type]
        made[0] += sum(len(word) for word in words or [])
        return words

    monkeypatch.setattr(argv_rules, "_expand_word", counting)
    for repeats in (100, 400):
        cmd = "a=(" + " ".join(["w" * 300] * 30) + "); git grep -O" + ref * repeats + " x"
        made[0] = 0
        assert _verdict(cmd) is None
        assert made[0] <= len(cmd), (ref, repeats, made[0], len(cmd))
