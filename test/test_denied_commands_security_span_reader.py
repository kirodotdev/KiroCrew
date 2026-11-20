"""The ssh-to-self floor's double-quoted ``$( )`` span reader skips needless heredoc re-reads.

``shell_commands._double_quoted_substitution_spans`` skips its heredoc re-read when no
heredoc body can reach a body's close. The skip changes no span, so no verdict: the
reader matches one that always re-reads, and lines main allows stay allowed.
"""

from __future__ import annotations

import random
import re

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _rule_pattern

from kiro_crew.security import is_denied
from kiro_crew.security import shell_commands as _shell_commands
from kiro_crew.security import shell_normalizer as _shell

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_ISSUE_BODY = "".join(f"line {i} of the issue body text here\n" for i in range(10))


def _gh_batch(n: int, repo: str) -> str:
    """*n* ``gh issue create`` calls, each with a 10-line quoted-heredoc body."""
    one = f"gh issue create --repo {repo} --title t --body \"$(cat <<'EOF'\n{_ISSUE_BODY}EOF\n)\""
    return "\n".join([one] * n)


def _always_reread(text):
    """The span reader as it was before the skip: every body re-reads the rest for heredocs."""
    spans, i, state = [], 0, 0
    while i < len(text):
        dollars = 0
        for step in _shell._iter_shell_chars(text[i:], state):
            at = i + step.offset
            dollars = dollars + 1 if step.state == 2 and step.text == "$" else 0
            if dollars % 2 and text.startswith("(", at + 1):
                rest = text[at + 2 :]
                for start, stop in _shell_commands.heredoc_text_spans(rest):
                    blank = re.sub("[^\n]", " ", rest[start:stop])
                    rest = rest[:start] + blank + rest[stop:]
                end, proven = _shell._matching_close_paren(rest, 0)
                spans.append((at, at + 2 + end if proven else len(text), proven))
                i, state = spans[-1][1], 2
                break
        else:
            break
    return spans


class TestSpanReaderSkip:
    @pytest.mark.parametrize(
        "line",
        [
            'echo "$(x)" ' * 800 + "; cat <<EOF\nssh\nEOF",
            'echo "$(basename "${f}")" ' * 200 + "; cat <<EOF\nssh\nEOF",
            "".join(f'echo "bit $(( 1 << {i} ))"\n' for i in range(110)) + "ls ~/.ssh\n",
            "".join(f'v{i}="$(jq -r .x <<< "$r")"\n' for i in range(110)) + "ls ~/.ssh\n",
        ],
        ids=["plain-bodies", "brace-bodies", "arith-shifts", "here-strings"],
    )
    def test_a_body_no_heredoc_can_reach_skips_the_heredoc_reread(self, monkeypatch, line):
        # A heredoc body starts after the newline that ends its ``<<`` line, so a body
        # whose close comes first (or a ``<<<`` / ``1 << n`` on one line) needs no
        # re-read. Counted, not timed: one re-read per body was the quadratic cost.
        reads = []
        real = _shell_commands.heredoc_text_spans
        with monkeypatch.context() as mp:
            mp.setattr(
                _shell_commands, "heredoc_text_spans", lambda text: reads.append(text) or real(text)
            )
            _shell_commands._double_quoted_substitution_spans(line)
        assert reads == []
        assert is_denied(line) is None

    @pytest.mark.parametrize(
        "line",
        [
            'x="$(cat <<E\n)\nE\n)"; echo "$(y)"',
            "x=\"$(cat <<-'E'\n\t)\n\tE\n)\" ; ssh -p 22 localhost id",
            'x="$(: ${v:-)}; cat <<E\nE\n)" ; cat <<E\nE',
            # The ``${``-aware walk closes later than the paren walk, and a heredoc sits
            # between the two closes: the skip must read up to the later one.
            'x="$(: ${v:-)}; cat <<E\n)\nE\n)" ; echo "$(y)"',
            'x="$(case a in a) cat <<E\nE\n;; esac)"; cat <<E\nE',
            'x="$(: # )\ncat <<E\n)\nE\n)"',
            'x="$(cat <<<y; echo "$(( 1 << 2 ))")" ; cat <<E\n)\nE',
            'echo "$(cat <<E\nx\nE\n)" "$(a)" "$(b <<< c)"',
            'x="$(cat <<E',
            _gh_batch(3, '"${REPO}"'),
            # Bodies longer than the first prefix, and a heredoc that closes only past it.
            'x="$(cat <<E\n' + "y" * 3000 + '\n)\nE\n)"; echo "$(a)"',
            'x="$(cat <<E\n' + "y\n" * 600 + 'E\n)"; cat <<E\n)\nE\n"$(b)"',
        ],
        ids=lambda line: line[:24],
    )
    def test_the_span_reader_matches_a_reader_that_always_rereads(self, line):
        for text in (line, line.lower(), "echo " + line):
            assert _shell_commands._double_quoted_substitution_spans(text) == _always_reread(text)

    @pytest.mark.parametrize("repo", ['"${REPO}"', "o/r"])
    @pytest.mark.parametrize("tail", ["", "\nls ~/.ssh"])
    def test_a_batch_of_heredoc_bodies_main_allows_stays_allowed(self, repo, tail):
        # 40 quoted-heredoc ``gh`` calls (about 17 KB): main allows this in about a second.
        # ``${`` (or a later ssh-family word) opens the body reader; it must not refuse.
        assert is_denied(_gh_batch(40, repo) + tail) is None

    def test_heredoc_bodies_before_a_quote_trick_stay_refused(self):
        # Heredoc-holding bodies take the full re-read, so a quote in a later body still
        # cannot pair with an outer quote and hide the ssh-to-self command after it.
        pad = "echo \"$(cat <<'E'\nt\nE\n)\" " * 20
        refusal = is_denied(pad + 'echo "$(cat \'a"b\')" ; ssh -p 22 localhost id')
        assert refusal is not None
        assert _rule_pattern("sandbox-escape-ssh-self") in refusal.splitlines()[0]

    def test_a_line_of_bodies_that_each_hold_a_heredoc_reads_a_prefix_per_body(self, monkeypatch):
        # Counted, not timed: the characters re-read must grow with the number of bodies,
        # not with bodies times line length.
        def chars(n):
            line = "".join("echo \"$(cat <<'E'\nt\nE\n)\"\n" for _ in range(n)) + "echo ssh"
            read = []
            real = _shell_commands.heredoc_text_spans
            with monkeypatch.context() as mp:
                mp.setattr(
                    _shell_commands, "heredoc_text_spans", lambda t: read.append(len(t)) or real(t)
                )
                _shell_commands._double_quoted_substitution_spans(line)
            return sum(read)

        assert chars(200) < 2.5 * chars(100)

    def test_the_span_reader_matches_a_reader_that_always_rereads_on_random_text(self):
        rng = random.Random(39106)
        pieces = [
            '"$(',
            ")",
            '"',
            "'",
            "<<E\n",
            "<<'E'\n",
            "<<-E\n",
            "E\n",
            "\n",
            "x",
            " ",
            "$(",
            "${",
            "}",
            ":-",
            "<<<",
            "$((",
            "))",
            "\\",
            "`",
            "#",
            "ssh ",
            "cat ",
            ";",
        ]
        for _ in range(3000):
            text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 60)))
            assert _shell_commands._double_quoted_substitution_spans(text) == _always_reread(text)

    def test_the_random_reader_check_crosses_the_prefix_size(self, monkeypatch):
        rng = random.Random(7)
        pieces = ['"$(cat <<E\n', 'E\n)"', "a" * 300 + "\n", "$(", ")", "<<'E'\n", "E\n"]
        for _ in range(300):
            text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 40)))
            assert _shell_commands._double_quoted_substitution_spans(text) == _always_reread(text)
