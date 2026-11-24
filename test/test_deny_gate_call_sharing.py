"""One deny judgement reads each text once, and reading once changes no verdict.

``security.call_views`` lets a pure reader keep its answers for the length of one
``is_denied`` call. Two things have to hold for that to be safe, and each has its
tests here.

The verdict is the same with the memo and without. Every corpus row is judged
twice: by ``is_denied`` as the gate calls it, and by the same function with no call
memo open, where every reader computes afresh. A reader that is not pure, or a
shared answer some caller edited, shows up as two verdicts for one command. The
corpus is the shortest command per refusal the gate's own tests exercise and a
spread of commands they expect to pass (``fixtures/deny_gate_call_sharing_corpus.json``,
inputs only), the security conductor's golden paths, and generated long commands.

Nothing outlives the call. The memo opens when a call starts and is dropped when it
returns, so no verdict can depend on an earlier one.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401

from kiro_crew.hooks import TOOL_DENY, HookManager
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import argv_floor_ssh_bodies as _bodies
from kiro_crew.security import is_denied, shell_normalizer
from kiro_crew.security.call_views import _CALL_VIEWS, once_per_call, sharing_views

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


@pytest.fixture(autouse=True)
def _no_remote_host_is_looked_up(_floor_monkeypatch):
    """A few corpus rows name a remote host, and the gate asks DNS whether a host is
    this machine. A test reaches no host but loopback, so that answer is fixed here,
    as the ssh rows' own tests fix it: not this machine."""
    _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)


_TEST_DIR = Path(__file__).parent
_FIXTURE = _TEST_DIR / "fixtures" / "deny_gate_call_sharing_corpus.json"
_GOLDEN_PATHS = (
    _TEST_DIR.parent
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "security-conductor"
    / "golden-paths.json"
)


def _python_heredoc(lines: int) -> str:
    body = "\n".join(
        f"print('line {i}:', compute_total(rows[{i}], scale=1.5))" for i in range(lines)
    )
    return f"python3 - <<'EOF'\nimport json\n{body}\nEOF"


def _one_line(chars: int) -> str:
    base = (
        "grep -rn --include='*.py' 'def compute_total' src/kiro_crew/dashboard src/kiro_crew/acp "
    )
    return (base * (chars // len(base) + 1))[:chars].rstrip()


_LONG_LINE = _one_line(1000)

#: Long commands, where one judgement asks the same reader about the same text most often.
_GENERATED = [
    *(_python_heredoc(lines) for lines in (1, 10, 50)),
    *(_one_line(chars) for chars in (100, 1000, 3000)),
    *(" && ".join(f"test -f build/out-{i}.json" for i in range(parts)) for parts in (2, 10, 50)),
    "bash <<'EOF'\n" + "\n".join(f"echo step {i} && ls -la out-{i}" for i in range(20)) + "\nEOF",
    "bash -c " + shlex.quote("; ".join(f"echo {i} | tee log-{i}.txt" for i in range(20))),
    " ; ".join(f'X{i}=$HOME/dir-{i}; ls "$X{i}"' for i in range(20)),
]


def _corpus() -> list[str]:
    rows = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    golden = json.loads(_GOLDEN_PATHS.read_text(encoding="utf-8"))["golden_paths"]
    commands = [
        *rows["refused"],
        *rows["allowed"],
        *(row["command_or_flow"] for row in golden if row["kind"] == "shell"),
        *_GENERATED,
    ]
    return list(dict.fromkeys(commands))


_CORPUS = _corpus()


def test_the_corpus_holds_refusals_passes_and_long_commands():
    """A corpus that lost a part would still pass every row it has left."""
    rows = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert len(rows["refused"]) >= 50 and len(rows["allowed"]) >= 100
    assert all(is_denied(command) for command in rows["refused"])
    assert len(_CORPUS) >= len(rows["refused"]) + len(rows["allowed"]) + len(_GENERATED)


@pytest.mark.parametrize("command", _CORPUS, ids=[f"row{i}" for i in range(len(_CORPUS))])
def test_a_shared_call_and_an_unshared_call_give_one_verdict(command):
    unshared = is_denied.__wrapped__(command)
    assert _CALL_VIEWS.get() is None
    assert is_denied(command) == unshared
    assert _CALL_VIEWS.get() is None


class _CountingShlex:
    """``shlex`` as the normalizer sees it, noting each ``(text, mode)`` handed to
    ``split``. The mode is the ``preserve_structure`` flag read from the
    ``_shell_tokens`` frame that calls ``split``: ``False`` is the default
    deny-matching tokenizer, ``True`` the escape-aware ssh-to-self descent."""

    def __init__(self) -> None:
        self.records: list[tuple[str, bool]] = []

    def split(self, text, *args, **kwargs):  # type: ignore[no-untyped-def]
        mode = bool(sys._getframe(1).f_locals.get("preserve_structure", False))
        self.records.append((text, mode))
        return shlex.split(text, *args, **kwargs)

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(shlex, name)


@pytest.mark.parametrize(
    "command",
    [
        "echo ok",
        "grep -rn foo src | sort | head -5",
        _LONG_LINE,
        "bash -c 'make lint && make test' 2>&1 | tee build.log",
        'OUT=$HOME/out; mkdir -p "$OUT" && ls "$OUT"',
        "ssh far.example.com id",
        "bash -c 'ssh far.example.com id'",
    ],
    ids=[
        "one-word",
        "pipeline",
        "long-line",
        "nested-shell",
        "assignment",
        "ssh",
        "descend-and-diverge",
    ],
)
def test_one_gate_call_tokenizes_each_text_once(monkeypatch, command):
    counting = _CountingShlex()
    monkeypatch.setattr(shell_normalizer, "shlex", counting)
    assert is_denied(command) is None
    assert counting.records
    from collections import Counter

    # One gate call tokenizes each (text, mode) at most once: the per-call memo serves
    # every repeat, so a text asked for twice in the SAME mode is split once. _shell_tokens
    # runs in two modes within a call -- the DEFAULT deny-matching tokenizer and the
    # escape-aware preserve_structure ssh-to-self descent. A per-TEXT len==len(set) form
    # would redden on the descend-and-diverge command, whose outer frame IS split in BOTH
    # modes (two records for one text); the (text, mode) form is the faithful successor:
    # each text split once in EACH mode, never twice in one mode.
    per_text_mode = Counter(counting.records)
    assert max(per_text_mode.values()) == 1, dict(per_text_mode)
    modes = {mode for _text, mode in counting.records}
    # The DEFAULT deny-matching tokenizer (mode False) runs for every command.
    assert False in modes
    # The escape-aware preserve_structure descent (mode True) runs ONLY when the fold scanner
    # both DESCENDS -- the command can name an ssh verb after folding (an ssh-family verb,
    # seen through quote/backslash/assignment splicing, or a ``\``-newline continuation) --
    # AND the command DIVERGES between the modes (carries a quote, so the shared-tokenizer
    # pre-check does not reuse the default split). A quote-free descending command reuses the
    # default split; a verb-free command is not descended. The memo bounds each (text, mode)
    # to one split either way, which is what this file pins.
    descends = _bodies._line_may_name_ssh_verb(command.lower()) or "\\\n" in command.lower()
    diverges = shell_normalizer._modes_may_diverge(command.lower())
    assert (True in modes) == (descends and diverges), (
        command,
        descends,
        diverges,
        dict(per_text_mode),
    )


def test_outside_a_call_a_marked_reader_computes_every_time():
    asked: list[str] = []

    @once_per_call()
    def reader(text: str) -> str:
        asked.append(text)
        return text.upper()

    assert _CALL_VIEWS.get() is None
    assert [reader("a"), reader("a")] == ["A", "A"]
    assert asked == ["a", "a"]


def test_inside_one_call_a_marked_reader_computes_each_text_once():
    asked: list[str] = []

    @once_per_call()
    def reader(text: str) -> str:
        asked.append(text)
        return text.upper()

    @sharing_views
    def call() -> list[str]:
        return [reader("a"), reader("b"), reader("a"), reader("")]

    assert call() == ["A", "B", "A", ""]
    assert asked == ["a", "b", ""]


def test_two_marked_readers_never_share_an_answer():
    @once_per_call()
    def upper(text: str) -> str:
        return text.upper()

    @once_per_call()
    def doubled(text: str) -> str:
        return text + text

    @sharing_views
    def call() -> tuple[str, str, str]:
        return upper("ab"), doubled("ab"), upper("ab")

    assert call() == ("AB", "abab", "AB")


def test_each_caller_gets_its_own_copy_of_a_mutable_answer():
    @once_per_call(fresh=list)
    def words(text: str) -> list[str]:
        return text.split()

    @sharing_views
    def call() -> tuple[list[str], list[str]]:
        first = words("a b")
        first.append("edited")
        second = words("a b")
        second.clear()
        return first, words("a b")

    first, third = call()
    assert first == ["a", "b", "edited"]
    assert third == ["a", "b"]


def test_an_answer_that_raised_is_not_kept():
    asked: list[str] = []

    @once_per_call()
    def reader(text: str) -> str:
        asked.append(text)
        raise ValueError(text)

    @sharing_views
    def call() -> None:
        for _ in range(2):
            with pytest.raises(ValueError):
                reader("a")

    call()
    assert asked == ["a", "a"]


def test_nothing_is_kept_between_calls():
    asked: list[str] = []

    @once_per_call()
    def reader(text: str) -> str:
        asked.append(text)
        return text

    @sharing_views
    def call() -> str:
        assert _CALL_VIEWS.get() == {}
        return reader("a") + reader("a")

    assert call() == "aa"
    assert _CALL_VIEWS.get() is None
    assert call() == "aa"
    assert asked == ["a", "a"]


def test_a_call_that_raises_drops_its_memo():
    @sharing_views
    def call() -> None:
        raise RuntimeError("the call failed")

    with pytest.raises(RuntimeError):
        call()
    assert _CALL_VIEWS.get() is None


def test_one_judgement_resolves_the_effective_deny_set_once(monkeypatch):
    """The enabled rule ids and the deny-rules tier read one resolution of the set."""
    resolved: list[object] = []
    real = HookManager._effective_denied

    def counting(self, ctx):  # type: ignore[no-untyped-def]
        resolved.append(ctx)
        return real(self, ctx)

    monkeypatch.setattr(HookManager, "_effective_denied", counting)
    command = "git status --short | head -5"
    result = HookManager().on_tool_call(
        command,
        tool_kind="execute",
        raw_params={"command": command, "cwd": "/tmp"},
        command=command,
        is_shell=True,
        kas_builtin_ids=True,
        harness_builtin_tool="execute_bash",
        session_key="dashboard:test-deny-set-once",
        agent="kirocrew",
        resolved_agent="kirocrew",
    )
    assert result.action != TOOL_DENY
    assert len(resolved) == 1


def test_two_gate_calls_share_no_entry_and_nothing_survives_either(monkeypatch):
    """The memo's life is one ``is_denied`` call: each call opens its own and drops it."""
    from kiro_crew.security import deny_gate

    memos: list[dict] = []
    real = deny_gate._deny_pattern_passes

    def noting(*args, **kwargs):  # type: ignore[no-untyped-def]
        memos.append(_CALL_VIEWS.get())
        return real(*args, **kwargs)

    monkeypatch.setattr(deny_gate, "_deny_pattern_passes", noting)
    assert is_denied("echo first-marker | sort") is None
    assert _CALL_VIEWS.get() is None
    assert is_denied("echo second-marker | sort") is None
    assert _CALL_VIEWS.get() is None
    first, second = memos
    assert first and second and first is not second
    assert any("first-marker" in str(key) for key in first)
    assert not any("first-marker" in str(key) for key in second)
    assert not any("second-marker" in str(key) for key in first)


# --- The shared-tokenizer construct pre-check (scanner reuses the default split) ----------
# ``_shell_tokens`` routes the structure-preserving mode to the default-mode memo entry when
# the command carries no construct on which the two modes can differ (no quote -> no empty-
# quote pair), so the two ssh-self legs split such a command ONCE instead of twice. These
# commands DO make the two modes differ, so the pre-check must NOT share them: a nested
# ``bash -c "..."`` whose structural escaped pair the default elision would truncate, and an
# escaped empty pair that surfaces an empty argument.
_MODE_DIVERGENT = [
    'bash -c \'true\nbash -c "x\\"\\""\'',
    'echo \\"\\"',
]


def test_the_shared_tokenizer_matches_the_unshared_over_the_corpus(monkeypatch):
    # In BOTH modes, the tokens with the pre-check active equal the tokens computed with it
    # disabled (every command split in its true mode), over the corpus and the divergent set.
    def unshared(cmd, mode):
        with monkeypatch.context() as m:
            m.setattr(shell_normalizer, "_modes_may_diverge", lambda c: True)
            return shell_normalizer._shell_tokens(cmd, preserve_structure=mode)

    for cmd in [*_CORPUS, *_MODE_DIVERGENT]:
        for mode in (False, True):
            assert shell_normalizer._shell_tokens(cmd, preserve_structure=mode) == unshared(
                cmd, mode
            ), (cmd, mode)


def test_sharing_a_mode_divergent_command_corrupts_the_preserve_tokens(monkeypatch):
    # Mutant: share EVEN where the modes differ (pre-check says "cannot differ" always). The
    # preserve mode then returns the default tokens for a divergent command -- a corrupted
    # memo -- so at least one divergent command's preserve tokens change.
    def true_preserve(cmd):
        with monkeypatch.context() as m:
            m.setattr(shell_normalizer, "_modes_may_diverge", lambda c: True)
            return shell_normalizer._shell_tokens(cmd, preserve_structure=True)

    reference = {cmd: true_preserve(cmd) for cmd in _MODE_DIVERGENT}
    monkeypatch.setattr(shell_normalizer, "_modes_may_diverge", lambda c: False)
    assert any(
        shell_normalizer._shell_tokens(cmd, preserve_structure=True) != reference[cmd]
        for cmd in _MODE_DIVERGENT
    )
