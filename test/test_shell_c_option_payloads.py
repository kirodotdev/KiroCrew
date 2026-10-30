"""A shell keeps reading options and redirections after ``-c``.

``bash -c -x '<script>'`` runs ``<script>``, not ``-x``: the script is the first word
after ``-c`` that is neither an option cluster (``-o`` / ``-xo`` take the next word
too) nor a redirection the outer shell strips (``<<EOF``, ``<<<word``, ``3<f``). The
nested-payload reader yields that word next to the first word after ``-c``, so every
floor that walks nested shells sees the script. These pin the extra reading, that it
only adds, and that it reads only a shell's own ``-c``.
"""

from __future__ import annotations

import pytest

from kiro_crew.security import shell_normalizer as sn
from kiro_crew.security.deny_gate import is_denied

PUSH = "git push origin main"

HIDDEN = [
    "bash -c -x '{p}'",
    "bash -c -e '{p}'",
    "bash -c -o pipefail '{p}'",
    "bash -c -xo pipefail '{p}'",
    "bash -c +e '{p}'",
    "bash -c -l '{p}'",
    "sh -c -e -u '{p}'",
    "bash -c - '{p}'",
    "bash -c -x <<<word '{p}'",
    "bash -c <<EOF '{p}'\nbody\nEOF",
    "bash -c <<-EOF '{p}'\n\tbody\nEOF",
    "bash -c << EOF '{p}'\nbody\nEOF",
    "bash -c >/dev/null '{p}'",
    "bash -c 2>&1 -x '{p}'",
    "bash -ec -x '{p}'",
    # a long option before ``-c``, and ``-O`` taking a name
    "bash --norc -c -x '{p}'",
    "bash --rcfile /dev/null -c -x '{p}'",
    "bash -O extglob -c -x '{p}'",
    # zsh's digit options
    "zsh -c -1 '{p}'",
    "zsh -c -x1 '{p}'",
    # an ``-o``/``-O`` takes a value word; two ``o`` take two
    "bash -c -oo pipefail errexit '{p}'",
    "bash -coo pipefail errexit '{p}'",
    "bash -c -o >'a b' pipefail '{p}'",
    "bash -O >'a b' extglob -c -x '{p}'",
    # a redirection glued onto an option token
    "bash -c -x>/dev/null '{p}'",
    "bash -c -x</dev/null '{p}'",
    "bash -c +e>/dev/null '{p}'",
    # a quoted redirection target with a blank or ``;`` is still stripped
    "bash -c -x >'/tmp/a b' '{p}'",
    "bash -c -x 2>'a;b' '{p}'",
    "zsh -c -onoclobber >'a b' '{p}'",
    "bash -c -x " + " ".join(f">'a{n} b'" for n in range(12)) + " '{p}'",
    # ... and before ``-c``, inside the shell's own run
    "bash >'a b' -c -x '{p}'",
    "bash -x 2>'a;b' -c -x '{p}'",
    "bash >'a b' -c -x >'c d' '{p}'",
    # a redirection between ``-o`` and its name, or after ``--``, is stripped too
    "bash -c -o 2>&1 pipefail '{p}'",
    "bash -c -- 2>&1 '{p}'",
    # ksh / zsh read a glued ``-o`` name; bash reads the next word
    "zsh -c -onoclobber '{p}'",
    "ksh -c -onoclobber '{p}'",
    "bash -c -ox pipefail '{p}'",
    "zsh -c -x -onoclobber '{p}'",
    # a quoted script that opens with a redirection is the script, not a redirection
    "bash -c -x '2>/dev/null {p}'",
    "bash -c -o pipefail '0<x {p}'",
    # the carrier cluster's own ``o`` takes the next word as its name
    "bash -co pipefail -x '{p}'",
    "bash -oc pipefail -x '{p}'",
]


@pytest.mark.parametrize("shape", HIDDEN)
def test_the_script_after_options_or_redirections_is_a_payload(shape: str) -> None:
    command = shape.format(p=PUSH)
    assert is_denied(command) is not None, command
    assert is_denied(shape.format(p="kirocrew token")) is not None


@pytest.mark.parametrize(
    "command",
    [
        "bash -c -x 'echo hi'",
        "bash -c -o pipefail 'make 2>&1 | tee build.log'",
        "bash -c -e <<<word 'npm test'",
        "bash -c -x 'git push origin my-feature'",
        "echo bash -c -x 'git status'",
        "printf '%s\\n' \"bash -c -x 'echo ok'\"",
        # a later stage's ``-c`` is a count flag, and its pattern is text
        "bash -c 'make' && grep -c -e 'kirocrew token' notes.txt",
        "sh -c 'cat log' | grep -c -i 'git push origin main' log",
        "bash -c 'make' && wc -c -l 'git push origin main'",
        "bash build.sh | grep -c -i 'git push origin main' log",
        # a shell NAME as another command's argument opens no option run
        "grep -c bash log && grep -c -e 'kirocrew token' notes.txt",
        "grep bash notes.txt && grep -c -e 'kirocrew token' notes.txt",
        "bash -c 'make'\ngrep -c -e 'kirocrew token' notes.txt",
        "grep env x && sort -S 3</dev/null 'kirocrew token'",
        # past a script file, ``-c -e '<text>'`` are the script's arguments
        "bash /dev/null -c -e 'kirocrew token'",
        # a printed example of the option spelling stays a mention
        "echo bash -c -o pipefail 'git push origin main'",
        'echo bash -c -x "git push origin main"',
        "printf '%s\\n' bash -c -e 'kirocrew token'",
        # digit and value words that are really the script's own arguments
        "bash -c 'echo hi' x 'git push origin main'",
        "bash -c 'x' -1",
        "sh -c -- -1",
        "nice -10 bash -c -x 'echo hi'",
    ],
)
def test_ordinary_commands_still_pass(command: str) -> None:
    assert is_denied(command) is None, command


def test_the_first_word_reading_is_kept() -> None:
    tokens = sn._self_tokens("bash -c -x 'echo a'")
    assert sn._nested_shell_payloads(tokens) == ["-x", "echo a"]


def test_an_option_argument_is_not_the_script() -> None:
    # ``-o`` takes ``pipefail``; the script is the word after it
    tokens = sn._self_tokens("bash -c -o pipefail 'echo a' arg0")
    assert "echo a" in sn._nested_shell_payloads(tokens)
    assert "pipefail" not in sn._nested_shell_payloads(tokens)
    assert "arg0" not in sn._nested_shell_payloads(tokens)


def test_a_run_of_ambiguous_redirections_is_read_once() -> None:
    # every shell word shares one carrier, and each ambiguous word is hopped once
    tokens = ["bash"] * 2000 + ["-c"] + [">a b"] * 2000 + [PUSH]
    payloads = sn._nested_shell_payloads(tokens)
    assert PUSH in payloads
    assert payloads.count(">a b") <= 2 * 2000 + 2000


def test_many_option_carriers_share_one_scan(monkeypatch: "pytest.MonkeyPatch") -> None:
    # ``bash -c -o bash -c -o ...``: each ``-o`` eats the next ``bash`` as its value and
    # every carrier's run reaches the command's end, which was O(n^2) before the memo.
    # Counted, not timed (a timed ratio measures the runner): the shared memo makes the
    # option reads grow linearly, so doubling n roughly doubles them, never quadruples.
    calls = {"n": 0}
    real = sn._shell_option_value_count

    def counting(token: str, *, bash_mode: bool) -> "int | None":
        calls["n"] += 1
        return real(token, bash_mode=bash_mode)

    monkeypatch.setattr(sn, "_shell_option_value_count", counting)

    def reads_for(n: int) -> int:
        calls["n"] = 0
        sn._nested_shell_payloads((["bash", "-c", "-o"] * n) + ["x"])
        return calls["n"]

    assert reads_for(2000) < 3 * reads_for(1000)


def test_a_run_of_options_stays_linear() -> None:
    # one backward pass: a long option run under many shell words must not re-walk
    tokens = ["bash"] * 4000 + ["-c"] + ["-x"] * 4000 + [PUSH]
    assert PUSH in sn._nested_shell_payloads(tokens)
