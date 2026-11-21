"""Tests that a heredoc body nothing runs is data to the self-subcommand floors.

``cat``, ``tee`` and a bare ``python -`` with no way to shell out only store or print
their body, so a cloud command written in it is a mention. A body a shell or
an interpreter runs, or one the outer shell expands, is still read as commands.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _NAME

from kiro_crew import security
from kiro_crew.security import is_denied

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_CLOUD = f"{_NAME} cloud launch --no-login"
_RULE = "self-protection-cloud"


def _floor(cmd: str) -> "str | None":
    """The floor id that refused *cmd*, or None when it is allowed."""
    verdict = is_denied(cmd)
    if verdict is None:
        return None
    return verdict.splitlines()[0].removeprefix(security.DENY_REASON_PREFIX)


@pytest.mark.parametrize(
    "cmd",
    [
        # The issue's repro: Python writes the text into a Markdown file.
        "python3 - <<'EOF'\nfrom pathlib import Path\n"
        f'Path("note.md").write_text("Pass `{_CLOUD}` to skip the sign-in.\\n")\nEOF',
        f"python3 <<'EOF'\nprint('{_CLOUD}')\nEOF",
        f"cat > note.md <<'EOF'\nRun `{_CLOUD}` first.\n{_CLOUD}\nEOF",
        f"cat <<EOF > note.md\n{_CLOUD}\nEOF",
        f"tee -a note.md <<'EOF'\n{_CLOUD}\nEOF",
    ],
)
def test_data_heredoc_body_is_allowed(cmd):
    assert _floor(cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        # A shell or interpreter runs the body.
        f"bash <<'EOF'\n{_CLOUD}\nEOF",
        f"sh -s <<'EOF'\necho hi\n{_CLOUD}\nEOF",
        f"perl <<'EOF'\n`{_CLOUD}`;\nEOF",
        f"python3 - <<'EOF'\nimport os\nos.system('`{_CLOUD}`')\nEOF",
        f"python3 - <<'EOF'\nimport subprocess\nsubprocess.run('`{_CLOUD}`', shell=True)\nEOF",
        f"python3 tool.py <<'EOF'\n{_CLOUD}\nEOF",
        # The body reaches a shell around the data program.
        f"cat <<'EOF' | sh\n{_CLOUD}\nEOF",
        f"bash -c \"$(cat <<'EOF'\n{_CLOUD}\nEOF\n)\"",
        f"cat > >(bash) <<'EOF'\n{_CLOUD}\nEOF",
        f"{{ cat <<'EOF'\n{_CLOUD}\nEOF\n}} | bash",
        # A word that only reads as cat may run a shell, and another command can
        # rebind the name before cat runs.
        f"cat=bash; $cat <<'EOF'\n{_CLOUD}\nEOF",
        f"/tmp/x/cat <<'EOF'\n{_CLOUD}\nEOF",
        f"hash -p /bin/sh cat; cat <<'EOF'\n{_CLOUD}\nEOF",
        f"shopt -s expand_aliases\nalias cat=sh\ncat <<'EOF'\n{_CLOUD}\nEOF",
        f"cat > a.md <<'EOF'\n{_CLOUD}\nEOF\ngit add a.md && git status",
        # The outer shell expands an unquoted body before cat sees it.
        f"cat > note.md <<EOF\n$({_CLOUD})\nEOF",
        f"python3 - <<EOF\nprint('`{_CLOUD}`')\nEOF",
        # A command after the body is not part of it.
        f"cat > note.md <<'EOF'\nnotes\nEOF\n{_CLOUD}",
        f"cat > note.md <<'EOF'\nnotes\\\nEOF\n{_CLOUD}",
        # A tag bash decodes before matching (ANSI-C, quoted letters, escape) can
        # close the body at a line the literal reader misses, leaving the next
        # command to run: the whole text keeps its old read (GPT 6.1 F1).
        "cat <<$'E\\x4fF'\nnotes\nEOF\nkirocrew cloud launch",
        'cat <<"E"OF\nnotes\nEOF\nkirocrew cloud launch',
        "cat <<E\\OF\nnotes\nEOF\nkirocrew cloud launch",
        # A python body that unpickles or otherwise runs an opaque call is not
        # proven inert, so it is not blanked (GPT 6.1 F2).
        "python3 - <<'EOF'\nimport pickle\n"
        f"pickle.loads(b\"cos\\nsystem\\n(S'`{_CLOUD}`'\\ntR.\")\nEOF",
        f"python3 - <<'EOF'\ngetattr(__builtins__, 'ev'+'al')('__import__(\"os\")')\n{_CLOUD}\nEOF",
        # A python body with a floored subcommand token in shell-program position,
        # beside a non-inert statement, must stay unblanked so the floor sees it.
        f"python3 - <<'EOF'\nresult = os.popen('x')\n{_CLOUD}\nEOF",
        f"python3 - <<'EOF'\nprint(__import__('os'))\n{_CLOUD}\nEOF",
        f"python3 - <<'EOF'\ndef f():\n    pass\n{_CLOUD}\nEOF",
    ],
)
def test_executed_heredoc_body_is_still_refused(cmd):
    assert _floor(cmd) == _RULE


@pytest.mark.parametrize(
    "body",
    [
        "print('hello')",
        "import os\nx = 1\nprint(x)",
        'from pathlib import Path\nPath("n").write_text("data")\n',
        'open("n", "w").write("data")',
        "y = [1, 2, 3]\nz = {'a': 1}",
        "print('x' + 'y')",
    ],
)
def test_python_body_is_data_accepts_inert(body):
    from kiro_crew.security import argv_floor_self as mod

    assert mod._python_body_is_data(body.lower())


@pytest.mark.parametrize(
    "body",
    [
        # A call on an assignment RHS, a nested call argument, a string holding '('
        # before a call (no line/bracket heuristic any more).
        "import os\nresult = os.system('x')",
        "print(__import__('os').system('x'))",
        "print('(')\nos.system('x')",
        'path("n").write_text(f"{os.popen(\'x\').read()}")',
        "import pickle\npickle.loads(b'...')",
        "getattr(__builtins__, 'system')('x')",
        "def f():\n    os.system('x')\nf()",
        "for i in range(1):\n    os.system('x')",
        "this is not python",
        "exec('os.system(\"x\")')",
        "eval('1')",
        # A name that shadows an accepted builtin makes a later call run it
        # (GPT 6.1 F1).
        "from os import system as print\nprint('x')",
        "import os as open\nopen('x')",
        "print = __import__('os').system\nprint('x')",
        # A tree too deep to classify cheaply keeps the old read (GPT 6.1 F2);
        # ast.parse raises on this, caught and read as not-data.
        "x = " + "1 + " * 1100 + "1",
        # A tuple / attribute / subscript assignment target can rebind an accepted
        # name or mutate an untracked object.
        "import os\nprint, unused = os.system, 0\nprint('x')",
        "import builtins, os\nbuiltins.print = os.system\nprint('x')",
        "import os\nd = {}\nd['f'] = os.system\nd['f']('x')",
        # A call hidden in an f-string format spec, and in an annotation, both run.
        "import os\nprint(f\"{1:{os.system('x')}}\")",
        "import os\nx: os.system('y') = 1",
    ],
)
def test_python_body_is_data_rejects_executable(body):
    from kiro_crew.security import argv_floor_self as mod

    assert not mod._python_body_is_data(body.lower())


_RESTART = f"{_NAME} restart"
_SSH = "ssh -p 22 localhost id"


def _quote_body_shapes(command: str, quote: str) -> "list[str]":
    """A data heredoc whose body holds one *quote*, then *command* on the line after
    the closing line, then a line that holds the matching quote."""
    return [
        f"cat <<'EOF'\nit{quote}s\nEOF\n{command}\n# {quote}",
        f"cat <<EOF\nit{quote}s\nEOF\n{command}\n# {quote}",
        f"cat <<-'EOF'\n\tit{quote}s\n\tEOF\n{command}\n# {quote}",
        f"tee out.txt <<'EOF'\nit{quote}s\nEOF\n{command}\n# {quote}",
        f"cat <<'A'\nit{quote}s\nA\ncat <<'B'\nok\nB\n{command}\n# {quote}",
    ]


@pytest.mark.parametrize(
    ("cmd", "rule"),
    [
        (cmd, rule)
        for command, rule in (
            (_RESTART, "self-protection-restart"),
            (_SSH, "sandbox-escape-ssh-self"),
        )
        for quote in ("'", '"')
        for cmd in _quote_body_shapes(command, quote)
    ]
    + [
        (f"cat <<'EOF'\nhello\nEOF\n{_RESTART}", "self-protection-restart"),
        (f"cat <<'EOF'\nhello\nEOF\n{_SSH}", "sandbox-escape-ssh-self"),
        # The floor does not model ``#`` comments, so a comment line after the body
        # reads as it does after a body with no quote in it.
        (
            f"cat > f.sh <<'EOF'\necho \"$(echo 'a\"b')\" # c\nEOF\n# {_SSH}",
            "sandbox-escape-ssh-self",
        ),
    ],
)
def test_line_after_closing_line_starts_unquoted(cmd, rule):
    # bash ends the body at its closing line, and reads the next line with no
    # quote open, whatever quote characters the body held.
    head = _floor(cmd)
    by_pattern = {r.pattern: r.id for r in security.BUILTIN_DENIED_RULES}
    assert by_pattern.get(head, head) == rule


@pytest.mark.parametrize(
    "cmd",
    [
        "cat <<'EOF'\nit's fine\nEOF",
        "cat <<'EOF'\nit's fine\nEOF\necho done",
        'cat <<EOF > notes.md\nsay "hi\nEOF\nls -la',
        "python3 - <<'EOF'\nprint('it''s', \"x\")\nEOF",
    ],
)
def test_quote_in_data_body_allows_ordinary_next_lines(cmd):
    assert _floor(cmd) is None


@pytest.mark.parametrize(
    ("cmd", "rule"),
    [
        ("s's'h -p 22 localhost id\ncat <<'EOF'\nit's\nEOF\n# '", "sandbox-escape-ssh-self"),
        (
            f"{_NAME[:4]}'{_NAME[4:]}' restart\ncat <<'EOF'\nit's\nEOF\n# '",
            "self-protection-restart",
        ),
    ],
)
def test_line_before_body_is_read_without_the_body_quote(cmd, rule):
    # The command line before a body is read up to the body, so a quote in the body
    # does not pair with a quote after the closing line around that line's words.
    head = _floor(cmd)
    by_pattern = {r.pattern: r.id for r in security.BUILTIN_DENIED_RULES}
    assert by_pattern.get(head, head) == rule


_SPELLED_RESTART = f"{_NAME[:4]}'{_NAME[4:]}' restart"
_SPELLED_SSH = "s's'h -p 22 localhost id"


def _nested_body_shapes(command: str) -> "list[str]":
    """A heredoc whose body holds one ``'``, inside a substitution or a process
    substitution, then *command* on a later line."""
    after = f"cat <<'B'\n'\nB\n{command}"
    return [
        f"echo \"$(cat <<'A'\n'\nA\n)\"\n{after}",
        f"echo \"`cat <<'A'\n'\nA\n`\"\n{after}",
        f"cat <(cat <<'A'\n'\nA\n)\n{after}",
        f"x=$(cat <<'A'\n'\nA\n)\n{command}",
    ]


@pytest.mark.parametrize(
    ("cmd", "rule"),
    [
        (cmd, rule)
        for command, rule in (
            (_SPELLED_RESTART, "self-protection-restart"),
            (_SPELLED_SSH, "sandbox-escape-ssh-self"),
        )
        for cmd in _nested_body_shapes(command)
    ],
)
def test_substitution_closes_after_a_heredoc_body_with_a_quote(cmd, rule):
    # bash reads a heredoc inside a substitution by lines too: a quote in its body
    # does not hide the ``)`` or backtick that closes the substitution.
    head = _floor(cmd)
    by_pattern = {r.pattern: r.id for r in security.BUILTIN_DENIED_RULES}
    assert by_pattern.get(head, head) == rule


@pytest.mark.parametrize(
    "cmd",
    [
        "x=$(cat <<'A'\nit's\nA\n)\necho \"$x\"",
        "echo \"$(cat <<'A'\nit's\nA\n)\"\ncat <<'B'\n'\nB\nls -la",
        "echo $(( 1 << 2 ))\necho 'a\nb'",
        "cat <<< 'it'\"'\"'s'\nls",
    ],
)
def test_heredoc_in_substitution_allows_ordinary_next_lines(cmd):
    assert _floor(cmd) is None


@pytest.mark.parametrize(
    ("cmd", "rule"),
    [
        (f'echo "$(cat <<{tag}\nnotes\n{close}\n{command}\n)"', rule)
        for command, rule in (
            (_RESTART, "self-protection-restart"),
            (_SSH, "sandbox-escape-ssh-self"),
        )
        for tag, close in (
            ("$'e\\x6ff'", "eof"),
            ("$'\\x65of'", "eof"),
            ("$'e\\157f'", "eof"),
            ("$'EOF'", "EOF"),
        )
    ],
)
def test_ansi_c_tag_closes_on_its_decoded_line(cmd, rule):
    # bash decodes the escapes of a ``$'...'`` tag before it compares lines with it,
    # so the body closes on the decoded line and the next line is a command.
    head = _floor(cmd)
    by_pattern = {r.pattern: r.id for r in security.BUILTIN_DENIED_RULES}
    assert by_pattern.get(head, head) == rule


def test_ansi_c_tag_allows_ordinary_next_lines():
    assert _floor("echo \"$(cat <<$'e\\x6ff'\nit's\neof\n)\"\nls -la") is None


def test_ssh_to_another_host_after_quoted_body_is_allowed(monkeypatch):
    # The host is not this machine; the stub keeps the test off real name lookups.
    from kiro_crew.security import argv_floor

    monkeypatch.setattr(argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)
    cmd = "cat > notes.md <<'EOF'\nit's done\nEOF\nssh deploy@build.example.com uptime"
    assert _floor(cmd) is None


@pytest.mark.parametrize(
    ("line", "cuts"),
    [
        # A body that opens inside a double quote is not cut: the walk crosses it.
        ("echo \"$(cat <<'E{i}'\nit's\nE{i}\n)\"\n", 0),
        # A body inside an unquoted substitution is cut where it starts and ends.
        ("x=$(cat <<'E{i}'\nit's\nE{i}\n)\n", 2),
    ],
)
def test_heredoc_pieces_walk_the_text_once(line, cuts):
    from kiro_crew.security import shell_normalizer

    command = "".join(line.format(i=i) for i in range(400))
    command += "cat <<'A'\nit's\nA\nls\n"
    reads = []

    class Counted(str):
        """Text that counts each character read from it, slices included."""

        def __getitem__(self, key):
            if isinstance(key, slice):
                return Counted(str.__getitem__(self, key))
            reads.append(key)
            return str.__getitem__(self, key)

    pieces = shell_normalizer._heredoc_line_pieces(Counted(command))
    assert "".join(pieces).replace(" ; ", "\n") == command
    assert len(pieces) == 400 * cuts + 3
    assert pieces[-1] == " ; ls ; "
    assert reads
    # The walk, body search included, reads each character a bounded number of
    # times, not once per body.
    assert len(reads) <= 3 * len(command)


@pytest.mark.parametrize(
    ("cmd", "rule"),
    [
        # A ``<<`` in a comment opens no body: the comment ends at the newline.
        (f"# <<2\n: '\n2\n'\n{command}\n# '", rule)
        for command, rule in (
            (_RESTART, "self-protection-restart"),
            (_SSH, "sandbox-escape-ssh-self"),
        )
    ]
    + [
        # A ``<<`` inside an arithmetic span that runs over several lines is a shift.
        (f": $((\n1 << 2\n))\n: '\n2\n'\n{command}\n# '", rule)
        for command, rule in (
            (_RESTART, "self-protection-restart"),
            (_SSH, "sandbox-escape-ssh-self"),
        )
    ]
    + [
        (f": # <<X\necho '\nX\n' ; {_RESTART} ; echo \\'", "self-protection-restart"),
        (f"((\nx = 1 << 2\n))\n: '\n2\n'\n{_RESTART}\n# '", "self-protection-restart"),
    ],
)
def test_shift_or_comment_opens_no_body(cmd, rule):
    # bash reads these ``<<`` as a comment or a left shift, so the quote that follows
    # pairs with the next quote and the command after it runs.
    head = _floor(cmd)
    by_pattern = {r.pattern: r.id for r in security.BUILTIN_DENIED_RULES}
    assert by_pattern.get(head, head) == rule


@pytest.mark.parametrize(
    ("cmd", "rule"),
    [
        (f"cat <<EOF\n'\neof\n'\nEOF\n{command}\n# '", rule)
        for command, rule in (
            (_RESTART, "self-protection-restart"),
            (_SSH, "sandbox-escape-ssh-self"),
        )
    ]
    + [
        (f"bash -c \"cat <<EOF\n'\neof\n'\nEOF\n{_RESTART}\n# '\"", "self-protection-restart"),
        (
            f'bash -c "cat <<\\"EOF\\"\n\'\neof\n\'\nEOF\n{_RESTART}\n# \'"',
            "self-protection-restart",
        ),
        (f"cat <<EOF\n'\neof\n'\nEOF\necho a \\\nb\n{_RESTART}\n# '", "self-protection-restart"),
    ],
)
def test_closing_line_matches_tag_case(cmd, rule):
    # bash compares a body's lines with its tag case-sensitively: ``EOF`` closes on
    # ``EOF`` only, and the line after it is a command.
    head = _floor(cmd)
    by_pattern = {r.pattern: r.id for r in security.BUILTIN_DENIED_RULES}
    assert by_pattern.get(head, head) == rule


@pytest.mark.parametrize(
    "cmd",
    [
        "echo a # a comment with << in it\ncat <<'EOF'\nit's\nEOF\nls -la",
        "echo $((\n1 << 2\n))\ncat <<'EOF'\nit's\nEOF\nls -la",
        "cat <<EOF\nit's\nEOF\nls -la",
        "cat <<Eof\nit's\nEof\nls -la",
    ],
)
def test_comment_shift_and_cased_tag_allow_ordinary_next_lines(cmd):
    assert _floor(cmd) is None
