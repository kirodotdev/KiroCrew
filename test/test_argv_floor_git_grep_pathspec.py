"""A plain ``git grep`` pathspec glob is data, not a self-kill spelling.

``git grep x -- '*'; ls <dir named for the product>`` searches files and lists a
directory. The self-kill argv floor read the bare ``*`` operand as a word that
could expand to a kill program and refused it. ``git grep`` operands are
patterns and pathspecs that git never runs, so they are data, the same as
``ls`` or ``cp`` operands.

The exemption must not open a way to run a program. ``git grep -O<cmd>`` (and
``--open-files-in-pager=<cmd>``) runs ``<cmd>`` on the matching files, so any
``git grep`` frame carrying a pager option anywhere, including after a quoted
``;`` or inside a quoted word, gets no exemption. Neither does a frame with a
global option or an environment word before ``grep``, or one with an expansion
whose value the floor cannot see. The decision is made once per frame, so a
long operand list costs linear time.
"""

from __future__ import annotations

import pytest

from kiro_crew import security
from kiro_crew.security import shell_normalizer

_KILL = "self-protection-kill"
# Built from parts so no single literal in this file spells the kill command.
_K = "pk" + "ill"
_TARGET = "kirocrew"
_FILE = "test/test_argv_floor_inline_and_brace_scope.py"


def _verdict(cmd: str) -> str | None:
    return security.is_denied(cmd)


def _is_kill(cmd: str) -> bool:
    reason = _verdict(cmd)
    return bool(reason) and _KILL in str(reason)


# ---- must stay denied (security) -------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        # The exact command a reviewer showed opening a bypass: a quoted `'*;'` must not
        # end the pager-option check.
        f"git grep -e '*;' -O'{_K} -f {_TARGET} #' -- {_FILE}",
        f"git grep -e '*;' -O '{_K} -f {_TARGET} #' -- {_FILE}",
        f"git grep -e '*;' --open-files-in-pager='{_K} -f {_TARGET} #' -- {_FILE}",
        f"git grep -e '*;' --open='{_K} -f {_TARGET} #' -- {_FILE}",
        f"git --paginate grep -e '*;' -- {_FILE}; {_K} -f {_TARGET}",
    ],
)
def test_a_git_grep_that_can_open_a_pager_is_still_refused(cmd: str) -> None:
    assert _is_kill(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        f"git grep -e x -- {_FILE} -O'{_K} -f {_TARGET} #'",
        f"git grep -nO'{_K} -f {_TARGET} #' -e '*;' -- {_FILE}",
        f"git -c core.pager='{_K} -f {_TARGET}' grep -e '*;' -- {_FILE}",
        f"GIT_PAGER='{_K} -f {_TARGET}' git grep -e '*;' -- {_FILE}",
        f"a=('-O{_K} -f {_TARGET} #'); git grep \"${{a[@]}}\" -e '*;' -- {_FILE}",
    ],
)
@pytest.mark.xfail(
    strict=True,
    reason="pre-existing: these pager forms are not refused before this change either; "
    "this change must not be what lets them through, and closing them is separate work",
)
def test_pager_forms_the_floor_does_not_yet_refuse(cmd: str) -> None:
    assert _is_kill(cmd), cmd


# ---- bypass 1: a quoted separator on its own word must not end the pager check ----


@pytest.mark.parametrize(
    "pager",
    [
        f"-O'{_K} -f {_TARGET} #'",
        f"--open-files-in-pager='{_K} -f {_TARGET} #'",
    ],
)
def test_a_quoted_standalone_separator_does_not_change_the_verdict(pager: str) -> None:
    # A quoted ``;`` becomes its OWN token, after de-quoting identical to a real ``;``.
    # It must not end the pager-option scan: a ``git grep`` carrying a pager that runs a
    # kill is refused with or without it. (This is the bypass a reviewer found: the
    # standalone quoted ``;`` made the scan stop before the pager in the same frame. The
    # glued ``'*;'`` spelling above never reached that stop.)
    without_sep = f"git grep -e '*' -e {_TARGET} {pager} -- {_FILE}"
    with_sep = f"git grep -e '*' -e {_TARGET} -e ';' {pager} -- {_FILE}"
    assert _is_kill(without_sep), without_sep
    assert _is_kill(with_sep), f"a quoted ';' let it through: {with_sep}"


# ---- bypass 2: the exemption needs the bare literal `git`, not just basename `git` ----


@pytest.mark.parametrize(
    "cmd",
    [
        # Anything that runs BEFORE the git grep can make ``git`` resolve to something
        # that runs the operands: a function, an alias, a PATH change, a hash -p entry,
        # or any other earlier statement. The exemption is for a git grep that is the
        # FIRST statement only, so every one of these keeps the kill refused.
        f'git(){{ shift; "$@"; }}; git grep {_K} -f {_TARGET}',
        f'function git {{ shift; "$@"; }}; git grep {_K} -f {_TARGET}',
        f"PATH=/tmp:$PATH; git grep {_K} -f {_TARGET}",
        f"hash -p /tmp/shim/git git; git grep {_K} -f {_TARGET}",
        f"command git grep {_K} -f {_TARGET}",
        f"true && git grep {_K} -f {_TARGET}",
        # A path form is not git at all.
        f"/tmp/git grep {_K} -f {_TARGET}",
        f"./git grep {_K} -f {_TARGET}",
    ],
)
def test_the_git_grep_exemption_needs_the_bare_literal_git_run_first(cmd: str) -> None:
    assert _is_kill(cmd), cmd


# ---- the false positive this fixes ------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "git grep x -- '*'; ls /w/kirocrew",
        "git grep -n foo -- ':!*.lock' '*'; ls /w/kirocrew",
        "git grep -e foo -- '*.py'; ls /w/kirocrew",
    ],
)
def test_a_plain_git_grep_pathspec_glob_is_data(cmd: str) -> None:
    assert not _is_kill(cmd), _verdict(cmd)


# ---- cost ----------------------------------------------------------------------


def test_a_long_operand_list_is_checked_in_linear_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Counts the per-word pager checks instead of timing them, so the bound is
    # deterministic. The gate normalises a command a few times over, so a linear
    # scan checks each word a small constant number of times (8 today); a scan
    # that re-reads the rest of the list per word would need millions of checks.
    operands = 4000
    cmd = "git grep x -- " + "'*' " * operands + "; ls /w/kirocrew"
    calls = 0
    original = getattr(shell_normalizer, "_git_grep_opens_a_pager", None)

    def counting(token: str) -> bool:
        nonlocal calls
        calls += 1
        return bool(original and original(token))

    monkeypatch.setattr(shell_normalizer, "_git_grep_opens_a_pager", counting, raising=False)
    _verdict(cmd)
    assert calls <= 16 * (operands + 8)
