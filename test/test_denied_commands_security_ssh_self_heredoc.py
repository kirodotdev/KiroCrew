"""Tests for how the ssh-to-self floor reads a here-document body.

A body that no program on the line can run as a shell command line is data, so
the floor does not read it as argv: a Python edit script's ``n = a * localhost``
is not a glob verb naming ``ssh`` and its target. The floor reads bodies through the
self floor's ``_heredoc_data_view``, the one heredoc reader. A body that a shell, a wrapper,
an unknown program or a piped command could run stays argv, and so does every
body on a line the shared reader may misread (arithmetic, compound syntax, a
continuation in a tag or an unquoted body, a quoted or ``$`` tag, a second heredoc).
The reader models neither a ``\\r`` nor bash's join of a ``<<`` line that ends in a
backslash. On a line with either, this floor reads the whole line as it did before
the shared view, and never trusts a body blanked there.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_RULE = "sandbox-escape-ssh-self"


@pytest.fixture(autouse=True)
def _pin_own_host_cache(request, _floor_monkeypatch):
    """The own-host pin of ``TestSandboxEscapeSshSelf``: a fixed name cache with the
    resolve-once latch set, so no resolver thread starts, and a DNS alias verdict
    of "not self", so no test resolves a real name."""
    own_mp = pytest.MonkeyPatch()
    request.addfinalizer(own_mp.undo)
    own = security.socket.gethostname().strip().lower()
    pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
    _floor_monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
    own_mp.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
    _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)


@pytest.mark.parametrize(
    "cmd",
    [
        # A Python edit script on stdin: a bare ``*`` (multiplication) before a
        # self-host word read as a glob verb naming ``ssh`` and its target.
        "python3 - <<'EOF'\nn = a * localhost\nEOF",
        "tee f <<'EOF' >/dev/null\nn = a * localhost\nEOF",
        # bash keeps a backslash-newline in a quoted-tag body literally and matches
        # the delimiter on the raw line, so cat prints the ssh line; nothing runs it.
        "cat <<'EOF'\ninert\\\ntext\nssh -p 22 localhost id\nEOF",
        # The tag word in a file name or a body sentence, in another case inside a
        # line, or a tag that is not ASCII letters: bash closes only on the tag line.
        "cat > release.md <<'MD'\nssh -p 22 localhost id\nMD",
        "cat > note.md <<'EOF'\nThe file ends at EOF.\nssh -p 22 localhost id\nEOF",
        "cat <<'EOF'\nssh -p 22 localhost id Eof\nEOF",
        # A body line that is the tag in another case: bash keeps the body open to
        # the tag as written, and the bodies are found on the words as written.
        "cat <<'EOF'\nEof\nssh -p 22 localhost id\nEOF",
        "cat <<'é'\nssh -p 22 localhost id\né",
        "cat <<'E-F'\nssh -p 22 localhost id\nE-F",
        "tee -a CHANGELOG.md <<'END-OF-ENTRY' >/dev/null\nssh -p 22 localhost id\nEND-OF-ENTRY",
        "cat > note.md <<'EOF.'\nssh -p 22 localhost id\nEOF.",
        # A redirect glued to an unquoted tag ends the tag.
        "cat <<EOF>note.md\nssh -p 22 localhost id\nEOF",
        "tee note.md <<EOF>/dev/null\nssh -p 22 localhost id\nEOF",
        # A continuation before the ``<<`` only joins the command line.
        "cat > note.md \\\n  <<'EOF'\nssh -p 22 localhost id\nEOF",
        # Finding B: a stored script whose own tab-indented ``<<-EOF`` closes on a
        # tabbed ``EOF``; the reader strips tabs only for a ``<<-`` tag, so the outer
        # quoted body stays open to its bare ``EOF`` and the store reads as data.
        "cat > scripts/upgrade.sh <<'EOF'\n#!/bin/sh\n"
        "\tcat <<-EOF\n\tssh -p 22 localhost id\n\tEOF\nEOF",
        "tee scripts/upgrade.sh <<'EOF' >/dev/null\n#!/bin/sh\n"
        "\tcat <<-EOF\n\tssh -p 22 localhost id\n\tEOF\nEOF",
    ],
)
def test_body_no_program_can_run_is_data(cmd):
    assert _denied_by(cmd) is None


#: Common heredoc forms: (opener, prefix of each body line, closing line). ``<<-``
#: strips leading tabs from the body and the closing line.
_COMMON_FORMS = [
    ("<<'EOF'", "", "EOF"),
    ('<<"EOF"', "", "EOF"),
    ("<<-EOF", "\t", "\tEOF"),
    ("<<PY", "", "PY"),
    ("<<END_OF_SCRIPT", "", "END_OF_SCRIPT"),
]
_DATA_COMMANDS = ["cat {op}", "cat > s.txt {op}", "tee f {op} >/dev/null"]


def _common_form_rows() -> "list[str]":
    rows = []
    for op, prefix, close in _COMMON_FORMS:
        for program in _DATA_COMMANDS:
            for payload in ("ssh -p 22 localhost id", "n = a * localhost"):
                rows.append(f"{program.format(op=op)}\n{prefix}{payload}\n{close}")
        if not prefix:  # Python cannot parse a tab-indented body the reader hands it
            rows.append(f"python3 - {op}\nn = a * localhost\n{close}")
    # A body line that ends in a backslash, in a quoted-tag body.
    for program in _DATA_COMMANDS:
        op = "<<'EOF'"
        rows.append(f"{program.format(op=op)}\necho a \\\nb\nssh -p 22 localhost id\nEOF")
    rows.append("python3 - <<'EOF'\nx = 1 + \\\n2\nn = a * localhost\nEOF")
    return rows


@pytest.mark.parametrize("cmd", _common_form_rows())
def test_common_heredoc_forms_are_data(cmd):
    """Each common tag spelling and a quoted body with a backslash-newline, fed to
    ``cat``, ``cat > f``, ``tee f`` and ``python3 -``, is read as data."""
    assert _denied_by(cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        # A shell reads the body as its script.
        "bash <<'EOF'\nssh localhost id\nEOF",
        "sudo sh <<'EOF'\nssh localhost id\nEOF",
        "cat <<'EOF' | bash\nssh localhost id\nEOF",
        "source /dev/stdin <<'EOF'\nssh localhost id\nEOF",
        # The body is stdin to code that can run it.
        "python3 -c 'import os,sys; os.system(sys.stdin.read())' <<'EOF'\nssh localhost\nEOF",
        "perl -e 'system(<STDIN>)' <<'EOF'\nssh localhost\nEOF",
        "python3 runner.py <<'EOF'\nssh localhost\nEOF",
        # The body is written to a file that a shell then runs.
        "cat > x.sh <<'EOF'\nssh localhost id\nEOF\nbash x.sh",
        "cat > x.sh <<'EOF'\nssh localhost id\nEOF\n./x.sh",
        # An unquoted body runs its substitutions, even for ``cat``.
        "cat <<EOF\n$(ssh localhost id)\nEOF",
        "cat <<EOF\n`ssh localhost id`\nEOF",
        # A real ssh command outside the body.
        "python3 - <<'EOF'\nprint(1)\nEOF\nssh localhost id",
    ],
)
def test_body_a_program_can_run_stays_argv(cmd):
    assert _denied_by(cmd) == _RULE


@pytest.mark.parametrize(
    "cmd",
    [
        # The heredoc command is piped.
        "cat <<'EOF' | cat\nssh -p 22 localhost id\nEOF",
        # Two heredocs on one line.
        "cat <<'A'\nx\nA\ncat <<'B'\nssh -p 22 localhost id\nB",
        # An ANSI-C quoted tag.
        "cat <<$'EOF'\ninert\nEOF\nssh -p 22 localhost id",
        # Arithmetic: bash reads ``<<`` as a left shift and runs the ssh line.
        "((cat<<z))\nssh -p 22 localhost id\nz",
        # A continuation in a tag or in an unquoted-tag body can change the tag or the
        # delimiter, so it keeps the bodies.
        "cat <<EO\\\nF\ninert\nEOF\nssh -p 22 localhost id",
        "cat <<EOF\nEO\\\nF\nssh -p 22 localhost id\nEOF",
        # A ``\r`` anywhere: the reader drops it from a tag but not from the closing
        # line, where bash keeps it, so it can blank a line bash runs.
        "cat <<'EOF'\r\nignored\r\nEOF\r\nssh -p 22 localhost id\n",
        "cat <<EOF\r\nignored\r\nEOF\r\nssh -p 22 localhost id\n",
        "cat <<-EOF\r\nignored\r\nEOF\r\nssh -p 22 localhost id\n",
        "tee f <<'EOF'\r\nignored\r\nEOF\r\nssh -p 22 localhost id\n",
        "cd . && cat <<'EOF'\r\nignored\r\nEOF\r\nssh -p 22 localhost id\n",
        "cat > release.md <<'MD'\r\nssh -p 22 localhost id\r\nMD\r\n",
        "cat <<EOF>note.md\r\nssh -p 22 localhost id\r\nEOF\r\n",
        # A body line that is the tag in another case: bash closes only on the tag as
        # written, so the bodies are found on the words as written, not lowered.
        "cat <<'EOF'\nn = a * localhost\neof\n> '\nEOF\nssh -p 22 localhost id\n# '",
        "cat <<\"EOF\"\nn = a * localhost\neof\n> '\nEOF\nssh -p 22 localhost id\n# '",
        "cat <<EOF\nn = a * localhost\neof\n> '\nEOF\nssh -p 22 localhost id\n# '",
        "cat <<-EOF\nn = a * localhost\neof\n> '\nEOF\nssh -p 22 localhost id\n# '",
        "tee f <<'EOF'\nn = a * localhost\neof\n> '\nEOF\nssh -p 22 localhost id\n# '",
        "cat <<'EOF'\nn = a * localhost\neof\n< '\nEOF\nssh -p 22 localhost id\n# '",
        "cat <<'EOF'\nn = a * localhost\neof\n2> '\nEOF\nssh -p 22 localhost id\n# '",
        # An opener line that ends in a backslash: the reader does not model bash's
        # join, so this floor keeps its own reading of the whole line (what it did on
        # origin/main) and does not trust a blanked body. A single-backslash
        # continuation, an escaped ``\\``, or a ``\`` in a comment all stay refused.
        "tee -a notes.md <<'EOF' \\\n  >/dev/null\nssh -p 22 localhost id\nEOF",
        "cat > f <<'EOF' \\\n  2>/dev/null\nssh -p 22 localhost id\nEOF",
        "tee -a notes.md <<'EOF' \\\r\n  >/dev/null\r\nssh -p 22 localhost id\r\nEOF\r\n",
        "cat <<'EOF' x\\\\\nEOF\nssh -p 22 localhost id\nEOF",
        "cat <<'EOF' # \\\nEOF\nssh -p 22 localhost id\nEOF",
        "cat <<'EOF' \\\n| bash\nssh -p 22 localhost id\nEOF",
        "tee f <<'EOF' \\\n| sh\nssh -p 22 localhost id\nEOF",
        "cat <<'EOF' \\\n; bash x\nssh -p 22 localhost id\nEOF",
        # A wrapper, an assignment prefix, or Python running code it was given.
        "env python3 - <<EOF\nssh -p 22 localhost id\nEOF",
        "A=1 python3 - <<EOF\nssh -p 22 localhost id\nEOF",
        "python3 -c x <<EOF\nssh -p 22 localhost id\nEOF",
        "python3 -m x <<EOF\nssh -p 22 localhost id\nEOF",
        "python3 -c x - <<'EOF'\nssh -p 22 localhost id\nEOF",
        # Python reads stdin as its program only when the line names ``-``.
        "python3 <<'EOF'\nssh -p 22 localhost id\nEOF",
        # Main's reader keeps these: it blanks a ``python`` body only for a bare
        # ``python [-]`` that is the only command on the line, so any option, a
        # leading ``cd``, a second command or a stored script keeps the body.
        "python3 -u - <<'PY'\nn = a * localhost\nPY",
        "python3 -u -B -s - <<'EOF'\nn = a * localhost\nEOF",
        "python3 -u <<'EOF'\nn = a * localhost\nEOF",
        "cd /w && python3 - <<'EOF'\nn = a * localhost\nEOF",
        "cat > s.py <<'EOF'\nn = a * localhost\nEOF\npython3 s.py",
        "cd /w && cat > s.py <<'EOF'\nn = a * localhost\nEOF\npython3 s.py",
        "python3 -c x - <<'EOF'\nn = a * localhost\nEOF",
        "python3 -m foo - <<'EOF'\nn = a * localhost\nEOF",
        "python3 -i - <<'EOF'\nn = a * localhost\nEOF",
        "python3 -I - <<'EOF'\nn = a * localhost\nEOF",
        "python3 -uc x - <<'EOF'\nn = a * localhost\nEOF",
        "python3 -u s.py <<'EOF'\nn = a * localhost\nEOF",
        "cd $X && python3 - <<'EOF'\nn = a * localhost\nEOF",
        "cd /w; rm x && python3 - <<'EOF'\nn = a * localhost\nEOF",
        "cd /w; python3 - <<'EOF'\nn = a * localhost\nEOF",
        "cd /w* && python3 - <<'EOF'\nn = a * localhost\nEOF",
        "cd '/w' && python3 - <<'EOF'\nn = a * localhost\nEOF",
        "cd /w || python3 - <<'EOF'\nn = a * localhost\nEOF",
        "cd /w 2>x && python3 - <<'EOF'\nn = a * localhost\nEOF",
        "cd /a && cd /b && python3 - <<'EOF'\nn = a * localhost\nEOF",
        "\\cd /w && python3 - <<'EOF'\nn = a * localhost\nEOF",
        "cd /w && bash <<'EOF'\nssh -p 22 localhost id\nEOF",
        "cat > s.py <<'EOF'\nn = a * localhost\nos.system(n)\nEOF\npython3 s.py",
        "cat > s.py <<'EOF'\nn = a * localhost\nEOF\npython3 -c x s.py",
        # Lowered, ``EOF`` closes the ``Eof`` store early; bash closes at ``Eof`` and
        # runs the ssh line.
        "cat > s.py <<'Eof'\nEOF\npython3 s.py\nEof\nssh -p 22 localhost id",
        "cat > s.py <<'Eof'\nEOF\ncat <<'x'\nEof\nssh -p 22 localhost id\nx",
        # A brace outside the bodies.
        "cat ${f} <<'EOF'\nssh -p 22 localhost id\nEOF",
        # A ``<<`` inside quotes still counts: the line holds two.
        "cat '<<' <<'EOF'\nssh -p 22 localhost id\nEOF",
        # Lowered, ``EOF`` closes the ``Eof`` body early and a second heredoc seems to
        # swallow the ssh line; bash closes at ``Eof`` and runs it.
        "cat <<'Eof'\nEOF\ncat <<'x'\nEof\nssh -p 22 localhost id\nx",
        # An unquoted body holds a ``$``.
        "cat <<EOF\n$x\nssh -p 22 localhost id\nEOF",
    ],
)
def test_line_the_reader_may_misread_keeps_bodies_as_argv(cmd):
    assert _denied_by(cmd) == _RULE


def test_argv_floor_imports_in_a_fresh_interpreter():
    """The ssh floor imports the self floor's heredoc view, and the facade's mirror refuses
    a name two owners bind to different objects, so the facade must still import whole."""
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src}
    proc = subprocess.run(
        [sys.executable, "-c", "import kiro_crew.security.argv_floor"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
