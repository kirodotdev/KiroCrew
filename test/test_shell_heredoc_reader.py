"""Here-documents in the shared reader (``read_commands(heredocs=True)``) and the rm
floor shapes that need them: a heredoc fed to a shell, a command after a heredoc, a
substitution in an unquoted body, and a decoded newline in ``$'...'`` given to ``-c``.

The wipe is spelled so the regex rows miss it and only the floor can refuse it.
"""

from __future__ import annotations

import pytest

from kiro_crew.security import rm_floor
from kiro_crew.security.deny_gate import is_denied
from kiro_crew.security.shell_commands import read_commands

RM = "r" + "m"
ROOT = f"{RM} -r --force /"
HOME = f"{RM} --recursive -f ~"


def _docs(text: str) -> "list[tuple[str, list[tuple[str, bool]]]]":
    return [
        (" ".join(word.raw for word in cmd.argv), [(d.body, d.quoted) for d in cmd.heredocs])
        for cmd in read_commands(text, heredocs=True)
    ]


class TestReader:
    def test_default_read_still_stops_at_a_heredoc(self):
        cmds = read_commands("cat <<EOF\nbody\nEOF\nnext")
        assert [c.separator for c in cmds] == ["<<"]
        assert all(c.heredocs == () for c in cmds)

    def test_bodies_go_to_their_command_and_the_read_goes_on(self):
        assert _docs("cat <<EOF | bash\nA\nB\nEOF\necho after") == [
            ("cat", [("A\nB\n", False)]),
            ("bash", []),
            ("echo after", []),
        ]

    def test_quoted_tags_strip_tabs_and_two_bodies_on_one_line(self):
        assert _docs("cat <<A <<\\B\n1\nA\n2\nB\nz")[0] == ("cat", [("1\n", False), ("2\n", True)])
        assert _docs("bash <<-EOF\n\tA\n\tEOF\ndone") == [
            ("bash", [("\tA\n", False)]),
            ("done", []),
        ]
        assert _docs("cat <<'EOF'\n$(x)\nEOF\nnext") == [
            ("cat", [("$(x)\n", True)]),
            ("next", []),
        ]

    def test_offsets_index_the_raw_text(self):
        text = "x; cat <<EOF\nhello\nEOF\n"
        doc = read_commands(text, heredocs=True)[1].heredocs[0]
        assert text[doc.start : doc.end] == doc.body == "hello\n"

    def test_an_unquoted_body_reads_each_substitution_once_with_literal_quotes(self):
        argvs = [a for a, _ in _docs("cat <<EOF\nit's $(id; who) and `date`\nEOF")]
        assert argvs[:4] == ["cat", "id", "who", "date"]

    def test_an_unclosed_body_runs_to_the_end(self):
        assert _docs("bash <<EOF\nA") == [("bash", [("A", False)]), ("", [])]


@pytest.mark.parametrize(
    "cmd",
    [
        pytest.param(f"bash <<EOF\n{ROOT}\nEOF", id="bash-heredoc"),
        pytest.param(f"bash <<'EOF'\n{ROOT}\nEOF", id="bash-quoted-heredoc"),
        pytest.param(f"bash <<-EOF\n\t{ROOT}\n\tEOF", id="bash-tab-heredoc"),
        pytest.param(f"bash -s <<EOF\n{HOME}\nEOF", id="bash-s-heredoc"),
        pytest.param(f"cat <<EOF | bash\n{ROOT}\nEOF", id="cat-heredoc-into-bash"),
        pytest.param(f"cat <<EOF\n$({ROOT})\nEOF", id="substitution-in-body"),
        pytest.param(f"cat <<EOF\n$(bash -c '{ROOT}')\nEOF", id="nested-shell-in-body"),
        pytest.param(f"cat <<EOF\nx\nEOF\n{ROOT}", id="command-after-heredoc"),
        pytest.param(f"bash -c $'echo\\n{ROOT}'", id="ansi-c-newline"),
        pytest.param(
            f"echo \"`cat <<EOF\nx`\"; bash -c '{ROOT}'", id="unclosed-heredoc-in-backtick"
        ),
        pytest.param(f"sudo -u root bash <<EOF\n{ROOT}\nEOF", id="wrapper-with-option-value"),
        pytest.param(f"bash -o posix -s <<EOF\n{ROOT}\nEOF", id="shell-option-with-value"),
        pytest.param(f"bash -euo pipefail <<'EOF'\n{ROOT}\nEOF", id="option-cluster-with-value"),
        pytest.param(f"cat <<EOF | chroot / sh\n{ROOT}\nEOF", id="piped-into-a-chroot-shell"),
        pytest.param(f"busybox sh <<EOF\n{ROOT}\nEOF", id="busybox-shell"),
        pytest.param(f"nsenter -t 1 -m bash <<EOF\n{ROOT}\nEOF", id="nsenter-shell"),
        pytest.param(f"unshare -r bash <<EOF\n{ROOT}\nEOF", id="unshare-shell"),
        pytest.param(f"flock /tmp/l bash <<EOF\n{ROOT}\nEOF", id="flock-shell"),
        pytest.param(f"taskset 1 bash <<EOF\n{ROOT}\nEOF", id="taskset-shell"),
        pytest.param(f"chrt 5 bash <<EOF\n{ROOT}\nEOF", id="chrt-shell"),
        pytest.param(
            f"cat <<EOF\n$(cat <<X\nyyyyyyyyyyyy\nX\n)\nEOF\nbash -c '{ROOT}'",
            id="command-after-nested-heredoc",
        ),
        # already refused before here-documents were read: kept so they stay refused
        pytest.param(f"bash -c -e '{ROOT}'", id="option-after-c"),
        pytest.param(f"bash -c -o posix '{ROOT}'", id="option-with-value-after-c"),
        pytest.param(f"bash -ce +c '{ROOT}'", id="option-cluster-after-c"),
        pytest.param(f"echo hi # x\\\nbash -c '{ROOT}'", id="comment-ending-in-backslash"),
        pytest.param(f"echo hi &# \\\nbash -c '{ROOT}'", id="background-comment-backslash"),
        pytest.param('echo "$(x=$((1<<2))\nrm -r -f ~\n)"', id="shift-quoted-substitution"),
        pytest.param('echo "`x=$((1<<2))\nrm -r -f ~\n`"', id="shift-quoted-backtick"),
        pytest.param("cat <<EOF\n$(x=$((1<<2))\nrm -r -f ~\n)\nEOF", id="shift-body-substitution"),
        pytest.param('echo "$( "$(x=$((1<<2))\nrm -r -f ~\n)" )"', id="shift-nested-substitution"),
        pytest.param("cat <<EOF\n`x=$((1<<2))\nrm -r -f ~\n`\nEOF", id="shift-body-backtick"),
        pytest.param(
            "cat <<EOF\n'$(x=$((1<<2))\nrm -r -f ~\n)'\nEOF", id="shift-body-literal-quotes"
        ),
        # with ``<<`` read as a shift, not a heredoc (no body follows it in bash)
        pytest.param(f"x=$((1<<2))\n{HOME}", id="shift-in-arith-expansion"),
        pytest.param(f"((1<<2))\n{HOME}", id="shift-in-arith-command"),
        pytest.param(f"((cat<<z))\n{HOME}\nz", id="shift-in-arith-command-tagged"),
        pytest.param(f"echo $((1<<2))\n{HOME}\n2", id="shift-in-arith-echo-tagged"),
        pytest.param(f"((1<<2)) && bash <<EOF\n{ROOT}\nEOF", id="heredoc-after-arith"),
        pytest.param(f"((1 # ))\ncat <<'EOF'\n'\nEOF\n{HOME} #'\n))", id="comment-in-arith"),
        pytest.param(
            f"((1 + $(cat <<'EOF' >/dev/null\n'\nEOF\nprintf 1\n) ))\n{HOME}",
            id="heredoc-in-subst-in-arith",
        ),
        pytest.param(f"x=$((1<<2))\n{RM} -r \\\n-f ~", id="arith-then-continued-wipe"),
        pytest.param(f"x=$((1<<2))\nbash -c '{HOME}'", id="arith-then-nested-wipe"),
        pytest.param(f"((1<<2))\nbash -c '{RM} -r \\\n-f ~'", id="arith-then-quoted-continuation"),
        pytest.param(f"x=$((1<<2))\necho hi # \\\n{HOME}", id="arith-then-comment-backslash"),
        pytest.param(f"x=$(\\\n(1<<2))\n{HOME}", id="arith-opener-split-by-continuation"),
        pytest.param(f"cat <<'EOF'\nok\nEOF\nx=$((1<<2))\n{HOME}", id="shift-after-a-heredoc"),
        pytest.param(f"x=$((1<<2))\n{HOME}\ncat <<'EOF'\nok\nEOF", id="shift-before-a-heredoc"),
        pytest.param(f"# don't\nx=$((1<<2))\n{HOME}", id="comment-quote-before-shift"),
        # a real heredoc body is data: its quotes, comments and parens open nothing
        pytest.param(
            f"cat > n <<'EOF'\ndon't\nEOF\nx=$((1<<2))\n{HOME}", id="body-quote-then-shift"
        ),
        pytest.param(f'cat <<EOF\nsay "hi\nEOF\nx=$((1<<2))\n{HOME}', id="body-dquote-then-shift"),
        pytest.param(f"cat <<EOF\na # (\nEOF\nx=$((1<<2))\n{HOME}", id="body-comment-paren-shift"),
        pytest.param(
            f"cat <<'EOF'\n((\nEOF\n# don't\n((1<<2))\n{HOME}", id="body-parens-then-shift"
        ),
        pytest.param(
            f"cat <<'EOF'\n$((1\nEOF\n# don't\nx=$((1<<2))\n{HOME}", id="body-open-arith-shift"
        ),
        pytest.param(
            f"cat <<A <<-'B'\n'\nA\n\t\"\n\tB\nx=$((1<<2))\n{HOME}", id="two-bodies-then-shift"
        ),
        pytest.param(f"x=$[1<<2]\ny=$((1<<2))\n{HOME}", id="old-arith-is-no-heredoc"),
        pytest.param(f'echo "$(printf ok)"; x=$((1<<2))\n{HOME}', id="quoted-subst-then-shift"),
    ],
)
def test_the_floor_refuses(cmd):
    assert rm_floor._rm_wipe_targets_nested(cmd)
    assert is_denied(cmd) is not None


@pytest.mark.parametrize(
    "cmd",
    [
        pytest.param(f"cat <<EOF\nbash -c '{ROOT}'\nEOF", id="body-given-to-cat"),
        pytest.param(f"cat > s.sh <<'EOF'\nbash -c '{ROOT}'\nEOF", id="script-written-to-a-file"),
        pytest.param(f"git commit -F - <<'EOF'\nnote: {ROOT} is refused\nEOF", id="commit-message"),
        pytest.param(f"cat <<'EOF'\n$({ROOT})\nEOF", id="quoted-body-substitution"),
        pytest.param(f"echo $'{ROOT}'", id="ansi-c-echo"),
        pytest.param(f"bash -c cat <<'EOF'\n{ROOT}\nEOF", id="shell-c-runs-cat"),
        pytest.param(f"tee bash <<'EOF'\n{ROOT}\nEOF", id="shell-name-as-a-file"),
        pytest.param(f"bash script.sh <<'EOF'\n{ROOT}\nEOF", id="shell-runs-a-script-file"),
        pytest.param(f"bash <<'EOF' </dev/null\n{ROOT}\nEOF", id="stdin-redirected-after"),
        pytest.param(f"bash -s -c cat <<'EOF'\n{ROOT}\nEOF", id="c-after-s-wins"),
        pytest.param(f"bash <<'A' 3<<'B'\nprintf ok\nA\n{ROOT}\nB", id="fd-3-heredoc-is-not-stdin"),
        pytest.param(f"cat <<'EOF' | bash </dev/null\n{ROOT}\nEOF", id="receiver-has-own-stdin"),
        pytest.param(f"bash -n <<'EOF'\n{ROOT}\nEOF", id="syntax-check-only"),
        pytest.param(f"bash -o noexec <<'EOF'\n{ROOT}\nEOF", id="noexec-shell"),
        pytest.param(f"cat <<EOF\nprefix\\\nEOF\n{ROOT}\nEOF", id="continuation-before-tag"),
        pytest.param(f"cat <<'EOF'\nEO\\\nF\n{ROOT}\nEOF", id="quoted-body-keeps-continuation"),
        pytest.param(f"printf 'true\\n' <<'EOF' | bash\n{ROOT}\nEOF", id="producer-ignores-stdin"),
        pytest.param(f"bash --help <<'EOF'\n{ROOT}\nEOF", id="shell-help-runs-nothing"),
        pytest.param(
            f'cat <<\'bash -c "{ROOT}"\'\nhello\nbash -c "{ROOT}"', id="tag-spells-a-wipe"
        ),
        pytest.param("echo '$(x=$((1<<2))\nrm -r -f ~\n)'", id="shift-single-quoted-data"),
        pytest.param("cat <<'EOF'\n$(x=$((1<<2))\nrm -r -f ~\n)\nEOF", id="shift-quoted-body-data"),
        pytest.param("echo '`x=$((1<<2))\nrm -r -f ~\n`'", id="shift-single-quoted-backtick-data"),
        pytest.param(
            "cat <<'EOF'\n`x=$((1<<2))\nrm -r -f ~\n`\nEOF", id="shift-quoted-body-backtick-data"
        ),
        pytest.param(
            "cat <<EOF\n\\$(x=$((1<<2))\nrm -r -f ~\n)\nEOF", id="shift-escaped-body-data"
        ),
        pytest.param(
            "cat <<EOF\n\\`x=$((1<<2))\nrm -r -f ~\n\\`\nEOF", id="shift-escaped-body-backtick-data"
        ),
        pytest.param("echo $((1<<2))", id="arith-shift-alone"),
        pytest.param("x=$((1<<2))\nrm -r -f ./build", id="arith-then-a-scoped-rm"),
        pytest.param(f"x=$(( `wc -l <<'EOF'\n{HOME}\nEOF\n` ))", id="arith-backtick-heredoc-data"),
        pytest.param(f"((cd b && make); cat > f <<'EOF'\n{ROOT}\nEOF\n)", id="arith-subshell-data"),
        pytest.param(f"((1<<2))\ncat <<'EOF'\n{HOME}\nEOF", id="heredoc-data-after-a-shift"),
        pytest.param(f"x=$(( $(cat <<'EOF'\n{HOME}\nEOF\n) ))", id="arith-subst-heredoc-data"),
        pytest.param(f"x=$((1<<2\n{HOME}", id="unclosed-arith-runs-nothing-after-it"),
        pytest.param(f"cat <<'EOF'\nx=$((1<<2))\n{HOME}\nEOF", id="shift-and-wipe-in-one-body"),
        pytest.param(f"cat <<EOF\nx=$((1<<2))\n{HOME}\nEOF", id="shift-and-wipe-unquoted-body"),
        pytest.param(
            f"x=$(( $( ((a) >o); cat <<'EOF'\n{HOME}\nEOF\necho 1) ))", id="subshells-in-arith-data"
        ),
        # a ``$((`` that is not arithmetic: its heredoc is real
        pytest.param(f"n=$((cat <<'EOF'\n{HOME}\nEOF\n) | wc -l)", id="failed-arith-subst-data"),
        # main's reading: a span that closes on a later line is not followed
        pytest.param(f"x=$((1<<2\n))\n{HOME}", id="shift-in-multiline-arith"),
        # main's reading: a ``((`` that closes as subshells keeps its heredoc
        pytest.param(f"((cat <<'EOF'\n{HOME}\nEOF\n); echo z)", id="heredoc-in-failed-arith"),
    ],
)
def test_the_floor_allows_body_data(cmd):
    assert rm_floor._rm_wipe_targets_nested(cmd) == set()
    assert is_denied(cmd) is None


def _nested_shell_heredocs(depth: int, body: str) -> str:
    head = "".join(f"bash <<E{i}\n" for i in range(depth))
    return head + body + "\n" + "".join(f"E{i}\n" for i in reversed(range(depth)))


def test_deep_heredoc_nesting_at_the_cap_stops_at_the_walk_budget(monkeypatch):
    # Each level's body is the whole rest of the text, so an unbounded walk re-reads it
    # once per level. Count the characters the walk reads, not the time it takes.
    from kiro_crew.security.paths import MAX_SCANNABLE_COMMAND_CHARS

    cmd = _nested_shell_heredocs(1180, RM)
    assert len(cmd) <= MAX_SCANNABLE_COMMAND_CHARS
    assert rm_floor._WALK_BUDGET_CHARS == 16 * MAX_SCANNABLE_COMMAND_CHARS
    read = []
    real = rm_floor._rm_wipe_targets
    monkeypatch.setattr(rm_floor, "_rm_wipe_targets", lambda s: read.append(len(s)) or real(s))
    raised = None
    try:
        rm_floor._rm_wipe_targets_nested(cmd)
    except RuntimeError as exc:
        raised = str(exc)
    assert sum(read) <= rm_floor._WALK_BUDGET_CHARS + len(cmd)
    assert raised is not None and "budget" in raised
    assert is_denied(cmd) is not None


def test_shallow_heredoc_nesting_is_refused_by_its_content():
    assert rm_floor._rm_wipe_targets_nested(_nested_shell_heredocs(2, HOME)) == {"home"}


_STEPS = "".join(f'echo "step {i}: $(date +%s)"\n' for i in range(200))


@pytest.mark.parametrize(
    "cmd",
    [
        ";".join(["((1<<2))"] * 2000) + ";rm -f scratch",
        "\n".join(["((1<<2))"] * 2000) + "\nrm -f scratch",
        "cat > notes.txt <<EOF\nhello\nEOF\n" + _STEPS,
    ],
    ids=["one-line", "many-lines", "heredoc-then-quoted-substitutions"],
)
def test_the_arithmetic_line_reading_stays_linear(monkeypatch, cmd):
    assert is_denied(cmd) is None
    read = rm_floor._commands.read_commands
    chars: list[int] = []

    def counting(text, *args, **kwargs):  # type: ignore[no-untyped-def]
        chars.append(len(text))
        return read(text, *args, **kwargs)

    monkeypatch.setattr(rm_floor._commands, "read_commands", counting)
    assert rm_floor._rm_wipe_targets_nested(cmd) == set()
    assert sum(chars) <= 12 * len(cmd)  # each line is read a fixed number of times


def _nested_failed_arith(depth: int) -> str:
    text = "true"
    for _ in range(depth):
        text = "n=$((cat <<E\nE\nprintf '%s' \"$(" + text + ')"\n) | cat)'
    return text + "; rm -f scratch"


def _nested_unclosed_arith(depth: int) -> str:
    text = "true"
    for _ in range(depth):
        text = "printf '%s' \"$(x=$((1<<2\n" + text + '\n)"'
    return text + "; rm -f scratch"


@pytest.mark.parametrize(
    "cmd",
    [_nested_failed_arith(13), _nested_unclosed_arith(13)],
    ids=["failed-arith-nesting", "unclosed-arith-nesting"],
)
def test_deep_arithmetic_nesting_reads_in_linear_calls(monkeypatch, cmd):
    # a span that does not close as arithmetic is read once, the way main reads it, so
    # the number of reads grows with the input, not doubling at each nesting level
    read = rm_floor._commands.read_commands
    calls: list[int] = []

    def counting(text, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(len(text))
        return read(text, *args, **kwargs)

    monkeypatch.setattr(rm_floor._commands, "read_commands", counting)
    assert rm_floor._rm_wipe_targets_nested(cmd) == set()
    assert len(calls) <= len(cmd)
