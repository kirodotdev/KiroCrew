"""The interpreter rows' inlined copy is measured before it is built, and a command
whose copy would grow past the budget is refused instead of read.

The two interpreter rows read a copy of the command in which every use of a name
bound to a quoted literal is that literal. One long literal whose name recurs grows
the copy by uses times length, and on main both rows then read all of it. Sizes and
counts, never a clock.
"""

from __future__ import annotations

import random
import re
import sys

import pytest
from denied_commands_security_helpers import (
    _NAME,
    _PK,
    _RULE_KILL,
    _RULE_MINT,
    _TOK,
    _assert_scan_is_linear,
    _denied_by,
    _interpreter_work,
    _linear_work,
)

from kiro_crew import security
from kiro_crew.security import deny_gate, is_denied
from kiro_crew.security.deny_waiver import deny_key_is_self_guard

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_BUDGET = 8 * 20 * 1024  # deny_gate._INTERP_COPY_CHAR_BUDGET, spelled out
_REFUSAL = "interpreter bindings add more than 163840 characters"
_CREW_PATH = "/srv/" + _NAME + "/logs"
_INTERPRETER_RULES = {_RULE_KILL + "-interpreter", _RULE_MINT + "-argv"}


def _main_copy(command: str) -> "tuple[str, str]":
    """The collapsed command and the copy main built from it, whatever its size.

    Main's three lines, kept here as the reference: lower-case, collapse the literal
    joins and ``%`` formats, then inline the bindings when the text holds ``crew``.
    """
    lower = command.lower()
    collapsed = deny_gate._collapse_percent_format(security._LITERAL_CONCAT_RE.sub("", lower))
    if "crew" not in collapsed:
        return collapsed, lower
    return collapsed, deny_gate._inline_interpreter_bindings(collapsed)


def _growth(command: str) -> int:
    collapsed, copy = _main_copy(command)
    return len(copy) - len(collapsed) if "crew" in collapsed else 0


def _without_budget(monkeypatch, command: str) -> "str | None":
    """``is_denied(command)`` with the budget lifted: the gate as it was on main."""
    with monkeypatch.context() as patch:
        patch.setattr(deny_gate, "_INTERP_COPY_CHAR_BUDGET", sys.maxsize)
        return is_denied(command)


def _copies_built(monkeypatch) -> "list[str]":
    """Every copy ``is_denied`` builds from here on, in order."""
    built: "list[str]" = []
    real = deny_gate._inline_interpreter_bindings

    def spy(text: str) -> str:
        copy = real(text)
        built.append(copy)
        return copy

    monkeypatch.setattr(deny_gate, "_inline_interpreter_bindings", spy)
    return built


def _at_budget(extra: int = 0) -> str:
    """A command whose bindings add exactly ``_BUDGET + extra`` characters.

    ``v`` is bound to a 513-character literal and read 320 times, its own binding
    included: 320 * 512 is the budget. ``ww='0'`` adds one, ``www=''`` takes one off.
    """
    command = "v='" + "0" * 511 + "'; echo " + " ".join(["v"] * 319) + "; ls " + _CREW_PATH
    if extra > 0:
        command = "ww='0'; " * extra + command
    if extra < 0:
        command = "www=''; " * -extra + command
    return command


def _reused_literal(words: int) -> str:
    """The class the issue is about: one quoted literal of short words bound to a
    name the words repeat, in a command that names a path holding ``crew``."""
    return "a='" + " ".join(["a"] * words) + "'; ls " + _CREW_PATH


# ── Generated commands ──────────────────────────────────────────────────────────

_NAMES = ("a", "n", "v", "cmd", "name_1", "k", "t")
_WORDS = (_NAME, _TOK, _PK, "-f", "x", "1", "kiro-crew", _CREW_PATH, "it's", 'say "hi"', "")
_SINKS = ("run", "subprocess.run", "os.system", "os.popen", "print", "os.kill", "log")
_BENIGN = (
    "cd " + _CREW_PATH + " && ls -la",
    "git -C ~/KiroCrew status --short",
    "grep -rn crew docs/ | head",
    "echo 'the crew met today'",
    "export A='a a a'; echo hi",
)
_REFUSED = (
    f"python3 -c \"import os; os.system('{_PK} -f {_NAME}')\"",
    f"python3 -c \"import os; os.system('p'+'kill -f {_NAME}')\"",
    f"python3 -c \"import subprocess; subprocess.run(['{_NAME}', '{_TOK}'])\"",
    f"python3 -c \"n='{_NAME}'; t='{_TOK}'; a='x'; run([n, a, a, a, t])\"",
    f"python3 -c \"k='{_PK}'; n='{_NAME}'; a='-f'; run([k, a, a, a, n])\"",
    f"python3 -c \"n='{_NAME}'; p='1'; os.kill(p, p, p, n\"",
    f"python3 -c \"os.system('%s -f %s' % ('{_PK}', '{_NAME}'))\"",
)


def _quoted(rng: random.Random, value: str) -> str:
    quote = "'" if '"' in value or ("'" not in value and rng.random() < 0.5) else '"'
    return quote + value + quote


def _generated(rng: random.Random) -> str:
    """One interpreter-style command: bindings, then calls that reuse the names."""
    statements = []
    names = rng.sample(_NAMES, rng.randint(1, 4))
    for name in names:
        value = rng.choice(_WORDS)
        if rng.random() < 0.3:  # a long literal, sometimes spelling its own name
            filler = rng.choice((name, "0", "ab", rng.choice(names)))
            value = " ".join([filler] * rng.choice((40, 300, 900)))
        statements.append(f"{name} = {_quoted(rng, value)}")
    for _ in range(rng.randint(1, 3)):
        uses = [rng.choice(names) for _ in range(rng.choice((1, 2, 5, 60, 400)))]
        if rng.random() < 0.3:
            uses.insert(rng.randrange(len(uses) + 1), _quoted(rng, rng.choice(_WORDS)))
        opener, closer = rng.choice((("([", "])"), ("(", ")"), ("(", "")))
        statements.append(rng.choice(_SINKS) + opener + ", ".join(uses) + closer)
    if rng.random() < 0.8:
        statements.append("open(" + _quoted(rng, _CREW_PATH) + ")")
    body = "; ".join(statements)
    wrapper = rng.choice(
        ("python3 -c {}", "node -e {}", "{}", "cd /tmp && python3 - <<EOF\n{}\nEOF")
    )
    if wrapper.startswith(("python3 -c", "node")):
        body = '"' + body.replace('"', "'") + '"'
    return wrapper.format(body)


def _corpus() -> "list[str]":
    rng = random.Random(34875)
    return [*_BENIGN, *_REFUSED, *(_generated(rng) for _ in range(100))]


class TestTheMeasureIsTheCopy:
    def test_the_growth_measured_is_the_growth_of_the_copy_main_built(self):
        grew = shrank = 0
        for command in (*_corpus(), "longname=''; echo longname " + _CREW_PATH):
            collapsed, _copy = _main_copy(command)
            built = deny_gate._inline_interpreter_bindings(collapsed)
            measured = deny_gate._inlined_growth(collapsed)
            assert measured == len(built) - len(collapsed), command[:200]
            grew += measured > 0
            shrank += measured < 0
        assert grew > 50 and shrank  # both directions were measured

    def test_measuring_builds_nothing_and_grows_with_the_text(self):
        work = []
        for words in (2000, 4000, 6000):
            text = _reused_literal(words)
            work.append(_interpreter_work(lambda text=text: deny_gate._inlined_growth(text)))
        assert _linear_work(work), work


class TestUnderTheBudgetEveryAnswerIsMains:
    def test_generated_commands_keep_their_answer(self, monkeypatch):
        built = _copies_built(monkeypatch)
        rows = security._INTERPRETER_RULE_PATTERNS
        under = over = by_interpreter_row = 0
        for command in _corpus():
            growth = _growth(command)
            del built[:]
            verdict = is_denied(command)
            if growth > _BUDGET:
                over += 1
                assert verdict is not None and _REFUSAL in verdict, command[:200]
                assert built == [], command[:200]
                continue
            under += 1
            assert _REFUSAL not in (verdict or ""), command[:200]
            assert all(len(copy) <= len(command) + _BUDGET for copy in built)
            assert verdict == _without_budget(monkeypatch, command), command[:200]
            by_interpreter_row += any(row in (verdict or "") for row in rows)
        # The corpus reaches both sides of the budget and both interpreter rows' copy.
        assert under > 70 and over > 10 and by_interpreter_row > 10

    @pytest.mark.parametrize("command", _REFUSED)
    def test_a_known_interpreter_payload_is_still_refused_by_its_row(self, command):
        assert _denied_by(command) in _INTERPRETER_RULES

    @pytest.mark.parametrize("command", _REFUSED[3:6])
    def test_a_row_matches_across_several_inlined_copies(self, command):
        # Why the copy cannot be searched in windows, or with repeats dropped: the
        # match starts in one inlined literal and ends several literals later.
        collapsed, copy = _main_copy(command)
        rows = security._INTERPRETER_RULE_PATTERNS
        assert not any(re.search(row, collapsed, re.IGNORECASE) for row in rows)
        assert any(re.search(row, copy, re.IGNORECASE) for row in rows)

    @pytest.mark.parametrize("command", _BENIGN)
    def test_a_benign_command_naming_the_path_is_allowed(self, command):
        assert is_denied(command) is None


class TestTheBudgetIsExact:
    @pytest.mark.parametrize("extra", [-1, 0])
    def test_a_copy_that_grows_by_the_budget_or_less_is_built_and_read(self, monkeypatch, extra):
        command = _at_budget(extra)
        assert _growth(command) == _BUDGET + extra
        built = _copies_built(monkeypatch)
        assert is_denied(command) is None
        assert [len(copy) for copy in built] == [len(command) + _BUDGET + extra]

    @pytest.mark.parametrize("extra", [1, 2, 500])
    def test_one_character_more_is_refused_before_the_copy_exists(self, monkeypatch, extra):
        command = _at_budget(extra)
        assert _growth(command) == _BUDGET + extra
        built = _copies_built(monkeypatch)
        assert is_denied(command) == (
            "Blocked by security policy: " + _REFUSAL + "\n"
            "a command that reuses a quoted value by name until the uses add more than"
            " 163840 characters is refused; shorten the value or run the script from a file"
        )
        assert built == []

    def test_the_spelled_out_numbers_are_the_gates(self):
        assert deny_gate._INTERP_COPY_CHAR_BUDGET == _BUDGET
        assert deny_gate._INTERP_COPY_REFUSAL == _REFUSAL


class TestPastTheBudgetNothingIsRead:
    def test_the_reported_class_is_refused_and_the_scan_stays_linear(self, monkeypatch):
        built = _copies_built(monkeypatch)
        # 3,000 to 9,000 words is 6 to 18 KiB, under the 20 KiB ceiling. Main built
        # copies of 18, 72 and 162 million characters for these.
        _assert_scan_is_linear(_reused_literal, (3000, 6000, 9000), denied=True)
        assert built == []
        for words in (3000, 6000, 9000):
            assert _REFUSAL in (is_denied(_reused_literal(words)) or "")

    def test_a_payload_a_row_would_refuse_stays_refused_past_the_budget(self):
        command = f"n='{_NAME}'; t='{_TOK}'; run([n, t]); " + _at_budget(1)
        assert _denied_by(f"n='{_NAME}'; t='{_TOK}'; run([n, t])") in _INTERPRETER_RULES
        assert _REFUSAL in (is_denied(command) or "")

    def test_no_override_waives_the_refusal(self):
        command = _at_budget(1)
        reason = is_denied(command)
        assert deny_key_is_self_guard(_REFUSAL)
        assert is_denied(command, waived=frozenset({_REFUSAL})) == reason

    def test_a_command_without_the_name_is_never_measured_or_refused(self, monkeypatch):
        measured = []
        real = deny_gate._inlined_growth
        monkeypatch.setattr(deny_gate, "_inlined_growth", lambda t: measured.append(t) or real(t))
        command = _at_budget(500).replace(_CREW_PATH, "/srv/logs")
        assert is_denied(command) is None
        assert measured == []

    def test_with_both_rows_switched_off_nothing_is_measured_or_refused(self, monkeypatch):
        built = _copies_built(monkeypatch)
        rows = security._INTERPRETER_RULE_PATTERNS
        enabled = [r.pattern for r in security.BUILTIN_DENIED_RULES if r.pattern not in rows]
        assert is_denied(_at_budget(500), denied_regexes=enabled) is None
        assert built == []
