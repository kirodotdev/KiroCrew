"""Liveness tests for the shell-command sensitive-path gate.

``is_sensitive_bash_command`` runs synchronously on the gateway's event loop,
under a loop-stall watchdog that hard-exits the process after 25 s of silence
(``dashboard.loop_stall_exit_after_secs``). A field crash (a cron whose agent
emitted a ~9 KB command full of ``https://`` URLs) traced to the gate: the
doubled separator in every URL sent the command through the separator-collapsed
re-scan, and the pattern tier was quadratic in the command. Three constructs
were each quadratic on their own and their costs multiply, so each has its own
pin here, plus the two trigger paths the crash dumps showed:

* Pass 1b -- ~10 KB with doubled separators (the URL / ``//`` shape);
* Pass 1  -- one 24 000-backslash run, which never reaches the loop at all.

Verdict pins are the cases where the rewritten construct was the ONLY branch
that matched, captured from the pre-rewrite gate on this tree and on the tree
before the anchor rewrite (#7941); a 381 474-command generated differential
corpus produced zero differences against both. Timing tests assert a doubling
RATIO where the code has no structure to observe, with the input well inside
the region where the old shape was already seconds, so a regression overshoots
by 4x while noise moves the linear result by a fraction.
"""

from __future__ import annotations

import inspect
import json
import time

import pytest

from kiro_crew import security
from kiro_crew.security import (
    MAX_SCANNABLE_COMMAND_CHARS,
    MAX_SCANNABLE_SOURCE_BODY_CHARS,
    _sensitive_pattern_hit,
    _verb_anchored_sensitive_hit,
    is_sensitive_bash_command,
)

# ─────────────────────────────────────────────────────────────────────────────
# Shapes
# ─────────────────────────────────────────────────────────────────────────────

_SEG = "a" * 60


def _double_separator_command(n: int) -> str:
    """n path operands, each with a doubled ``//`` so Pass 1 misses and the
    separator-collapsed re-scan (Pass 1b) runs -- the crash shape."""
    return "ls " + " ".join(f"/opt//{_SEG}" for _ in range(n))


def _url_payload_command(n: int) -> str:
    """The field shape: a JSON body of ``https://`` URLs handed to curl."""
    urls = [f"https://tasks.example.test/T{100000 + i}?view=full&x={_SEG[:20]}" for i in range(n)]
    return "curl -s -X POST -d " + json.dumps({"items": urls})


def _backslash_run_command(n: int) -> str:
    """One separator run of n backslashes: the Pass 1 path, no re-scan."""
    return "echo " + "\\" * n


def _unc_chain_command(n: int) -> str:
    """One UNC token carrying n canonical no-op hops (``\\X\\..``)."""
    return "type \\\\srv\\share" + "".join(f"\\{_SEG}\\.." for _ in range(n))


def _verb_dense_command(n: int) -> str:
    return " ".join(f"cat {_SEG}" for _ in range(n))


# ─────────────────────────────────────────────────────────────────────────────
# Verdict pins: the rewritten constructs accept the same language
# ─────────────────────────────────────────────────────────────────────────────

# Every path here is preceded by a character that is NOT in the token anchor
# class, so the verb-independent branch cannot be what matches: these were
# matched by the verb-anchored regex branch alone, and must still be denied.
VERB_ANCHORED_ONLY: list[str] = [
    "cat $(echo x)/home/user/.aws/credentials",
    "tee foo/home/user/.aws/credentials",
    "CAT foo/home/user/.aws/credentials",
    "git checkout -- x)/home/user/.ssh/id_rsa",
    "cat ./..//home/user/.aws/credentials",
    "sed -i s/a/b/ x}/Users/bob/.gnupg/secring.gpg",
    "echo a; cat b; ls x/home/user/.aws/credentials",
    'python -c "open(prefix/home/user/.aws/credentials)"',
    'PYTHON3 -c "open (x/home/user/.aws/credentials)"',
    "perl -e 'open(F, x/home/user/.ssh/id_rsa)'",
    "cat a\ncat x/home/user/.aws/credentials",
    "cat x/home/user/.aws/credentials\necho done",
    "cat x/home/user/.aws/credentials\r\necho done",
]

# The same shapes with the ordering or the line broken: the old branch did not
# match these (``.*`` never crosses a newline; ``open(`` must FOLLOW the
# interpreter), and neither may the rewrite.
VERB_ANCHORED_NEGATIVE: list[str] = [
    "cat foo\nx/home/user/.aws/credentials",
    "echo x/home/user/.aws/credentials",
    'python -c "print(x/home/user/.aws/credentials)"',
    "open(x/home/user/.aws/credentials) python -c 1",
    "python -c 1\nopen(x/home/user/.aws/credentials)",
    "concatenate x/home/user/.aws/credentials",
    "cat x/home/user/.awsx/credentials",
    # the path BEFORE the verb on the same line: the old branch required the
    # verb first, so the search from the verb's end must not look backwards
    "echo x/home/user/.aws/credentials; cat foo",
    "ls x/home/user/.aws/credentials && tee out",
    "print(open(x/home/user/.aws/credentials)); python -c 1",
]

REDIRECT_FORM: list[tuple[str, bool]] = [
    (">~/.aws/credentials", True),
    ("2>/home/user/.aws/credentials", True),
    ("echo x|~/.ssh/id_rsa", True),
    ("echo x >   $HOME/.aws/credentials", True),
    # a redirect somewhere earlier does not make a mid-token path a hit
    ("echo x >x/home/user/.aws/credentials", False),
]

UNC_FORM: list[tuple[str, bool]] = [
    (r"type \\server\share\.aws\credentials", True),
    (r"type \\server\share\sub\dir\.ssh\id_rsa", True),
    (r"type \\server\share\a\..\b\..\.kiro\crew\security_policy.json", True),
    (r"type \\server\share\.\.aws\credentials", True),
    (r"type \\server\.aws\credentials", True),
    (r"echo forged > \\server\share\.kiro\crew\connections-tool-aliases.json", True),
    (r"type \\server\share\.kiro\crew\%F%", True),
    (r"type \\server\share\.kiro\agents\x.json", True),
    (r"type \\server\share\\.aws\credentials", True),
    (r"type \\server\share\notes.txt", False),
    (r"type \\srv\x", False),
]


@pytest.mark.parametrize("command", VERB_ANCHORED_ONLY)
def test_verb_anchored_form_still_denies(command: str) -> None:
    assert is_sensitive_bash_command(command) is not None
    # And it is the verb-anchored half that carries it: the compiled
    # alternation alone does not match (the path is mid-token).
    assert _verb_anchored_sensitive_hit(command) is True
    assert security._get_sensitive_re().search(command) is None


@pytest.mark.parametrize("command", VERB_ANCHORED_NEGATIVE)
def test_verb_anchored_form_stays_allowed_when_order_or_line_breaks(command: str) -> None:
    assert is_sensitive_bash_command(command) is None
    assert _verb_anchored_sensitive_hit(command) is False


@pytest.mark.parametrize(("command", "expected"), REDIRECT_FORM)
def test_redirect_form_verdicts_unchanged(command: str, expected: bool) -> None:
    assert bool(is_sensitive_bash_command(command)) is expected


@pytest.mark.parametrize(("command", "expected"), UNC_FORM)
def test_unc_anchor_verdicts_unchanged(command: str, expected: bool) -> None:
    assert bool(is_sensitive_bash_command(command)) is expected


def test_pattern_tier_is_the_union_of_both_halves() -> None:
    """A verb-anchored-only hit and an anchor-only hit each flip the tier on
    their own, so dropping either half from ``_sensitive_pattern_hit`` fails."""
    assert _sensitive_pattern_hit("cat $(echo x)/home/user/.aws/credentials") is True
    assert _sensitive_pattern_hit("FOO=~/.aws/credentials") is True
    assert _verb_anchored_sensitive_hit("FOO=~/.aws/credentials") is False
    assert _sensitive_pattern_hit("ls -la") is False


# ─────────────────────────────────────────────────────────────────────────────
# Source guards: the quadratic spellings must not come back
# ─────────────────────────────────────────────────────────────────────────────


def test_compiled_alternation_carries_no_verb_or_redirect_wildcard() -> None:
    source = inspect.getsource(security._build_sensitive_regex)
    # Code spellings only (``rf"`` prefix), so the explanatory comment that quotes
    # the old form in prose is not counted.
    assert 'rf"(?:(?:' not in source, "the verb/redirect group is back in the regex"
    assert r'rf"(?:[<>|]\s*{sensitive_path}' in source
    for verbs in ("{_READ_CMDS}.*", "{_WRITE_CMDS}.*", "{_SCRIPT_OPEN}.*"):
        assert verbs not in source, f"the verb-anchored branch is back in the regex: {verbs}"
    # The UNC anchor takes a plain separator, never the generalized one.
    assert "{unc_prefix}{win_sep}" in source
    assert "{unc_prefix}{win_gsep}" not in source
    assert "|{unc_prefix}|" not in source, "unc_prefix is back inside win_home_alts"


# ─────────────────────────────────────────────────────────────────────────────
# Size ceiling: refused, not scanned, not skipped
# ─────────────────────────────────────────────────────────────────────────────


def test_oversized_command_is_refused_with_a_reason() -> None:
    cmd = "echo " + "x" * MAX_SCANNABLE_COMMAND_CHARS
    reason = is_sensitive_bash_command(cmd)
    assert reason is not None
    assert "too large to security-scan" in reason
    assert str(len(cmd)) in reason


def test_command_at_the_ceiling_is_scanned_not_refused() -> None:
    body = "x" * (MAX_SCANNABLE_COMMAND_CHARS - len("echo "))
    assert is_sensitive_bash_command("echo " + body) is None
    # And a sensitive path at the very end of a ceiling-sized command is found.
    tail = " ~/.aws/credentials"
    cmd = "echo " + "x" * (MAX_SCANNABLE_COMMAND_CHARS - len("echo ") - len(tail)) + tail
    assert len(cmd) == MAX_SCANNABLE_COMMAND_CHARS
    assert is_sensitive_bash_command(cmd) == "Blocked: command accesses sensitive credential path"


def test_ceiling_matches_the_tool_input_tier() -> None:
    """The two tiers refuse at the same size, so a command cannot be too long
    for one and scanned by the other."""
    from kiro_crew import llm_helpers

    assert llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS == MAX_SCANNABLE_COMMAND_CHARS


# ─────────────────────────────────────────────────────────────────────────────
# A cron SCRIPT BODY has its own ceiling, and is not a shell subject at all
# ─────────────────────────────────────────────────────────────────────────────


def test_the_source_body_ceiling_is_larger_and_owned_by_the_cron_reader() -> None:
    """20 KiB of shell on one ``Bash`` call is a heredoc; 20 KiB of cron script is an
    ordinary script, and refusing it there is permanent (every tick until edited). The
    cron gate reads and refuses on ONE number so the reader and the scan agree."""
    from kiro_crew import mcp_cron

    assert MAX_SCANNABLE_SOURCE_BODY_CHARS > MAX_SCANNABLE_COMMAND_CHARS
    assert mcp_cron._MAX_SCRIPT_SCAN_BYTES == MAX_SCANNABLE_SOURCE_BODY_CHARS

    body = "".join(f'value_{i} = "{"t" * 200}"\n' for i in range(120))
    assert MAX_SCANNABLE_COMMAND_CHARS < len(body) <= MAX_SCANNABLE_SOURCE_BODY_CHARS
    assert mcp_cron._vet_script_contents(body) is None
    assert mcp_cron._vet_script_contents(body + 'open("~/.aws/credentials")\n') is not None

    over = "x = 1\n" * MAX_SCANNABLE_SOURCE_BODY_CHARS
    reason = mcp_cron._vet_script_contents(over)
    assert reason is not None and "too large to security-scan" in reason


def test_the_shell_gate_has_no_source_body_entry_point() -> None:
    """RATCHET: ``is_sensitive_bash_command`` takes a shell command line and nothing
    else -- no subject flag, no re-pointed traversal subjects, no per-caller ceiling.
    Every one of those knobs existed once to make a Python source body survive a
    shell-grammar pass, and each pass still produced a false-denial class on ordinary
    scripts (#7912, #8563, #8643). A source body is not this gate's subject; see
    ``mcp_cron._vet_script_contents``."""
    params = inspect.signature(security.is_sensitive_bash_command).parameters
    assert set(params) == {"command", "enabled_ids"}, sorted(params)
    for name in (
        "is_sensitive_source_body",
        "_source_command_subjects",
        "_sensitive_run_in_source_literals",
        "_parse_source_body",
        "_SOURCE_PATTERN_SINKS",
        "_SOURCE_COMMAND_SUBJECT_CAP",
    ):
        assert not hasattr(security, name), name


# ─────────────────────────────────────────────────────────────────────────────
# Complexity: both trigger paths, and each construct on its own
# ─────────────────────────────────────────────────────────────────────────────


def _gate_seconds(command: str) -> float:
    started = time.perf_counter()
    is_sensitive_bash_command(command)
    return time.perf_counter() - started


def _pattern_seconds(command: str, attempts: int = 3) -> float:
    """The pattern tier alone (compiled alternation + verb walk), including the
    separator-collapsed re-scan, exactly as Pass 1 / 1b run it.

    The linearity pins below lift the size ceiling to reach input sizes where
    the algorithm dominates the noise, and at 80-100 KB the WHOLE gate is no
    longer about the constructs under test: pass 2 hands every path operand to
    the realpath pool, whose cost is the host filesystem's (a Windows CI runner
    spent 6.7 s there on a 1 280-operand command against 0.5 s on Linux). The
    constructs this file guards all live in the pattern tier, so that is what
    the ratio measures; the whole gate is pinned at the crash size, under the
    ceiling, where it is what the loop actually pays.

    The BEST of several attempts is the sample, because the quantity being
    measured has one direction of error: a shared runner with a sibling xdist
    worker on the same core can only ever make a scan look slower, never faster,
    so the minimum is the closest reading to the work the algorithm actually
    does. Wall clock rather than ``process_time``: the claim is about how long a
    caller is held, and ``process_time`` advances on the scheduler tick
    (~15.6 ms on Windows), which would quantise the smaller sample here onto one
    or two ticks and put the ratio at the mercy of rounding.
    """
    best = float("inf")
    for _ in range(attempts):
        started = time.perf_counter()
        if not _sensitive_pattern_hit(command):
            for collapsed in security._separator_collapsed_variants(command):
                if _sensitive_pattern_hit(collapsed):
                    break
        best = min(best, time.perf_counter() - started)
    return best


def test_pass1b_double_separator_10kb_is_fast() -> None:
    """The crash shape, at the crash size. 15 s on the shipped build, ~70 ms
    after the rewrite; the ceiling clears the fixed path by ~30x and the old
    one overshoots by ~7x."""
    cmd = _double_separator_command(160)
    assert 10_000 < len(cmd) <= MAX_SCANNABLE_COMMAND_CHARS
    assert security._separator_collapsed_variants(cmd), "shape must enter Pass 1b"
    assert _gate_seconds(cmd) < 2.0


def test_url_payload_12kb_is_fast() -> None:
    cmd = _url_payload_command(160)
    assert 10_000 < len(cmd) <= MAX_SCANNABLE_COMMAND_CHARS
    assert security._separator_collapsed_variants(cmd), "URLs must enter Pass 1b"
    assert _gate_seconds(cmd) < 2.0


def test_verb_anchored_backslash_run_under_the_ceiling_is_fast() -> None:
    """A verb followed by one long separator run, just under the ceiling: the
    shape that enters ``_verb_anchored_sensitive_hit``, whose path search is
    UNANCHORED from the verb's end. A generalized separator on the UNC anchor
    made that search quadratic in the run (91 s at 19 KB on the pre-rewrite
    pattern); the plain ``win_sep`` anchor makes it a no-op. Absolute bound on
    purpose: ratio pins on this file have flaked, and a 19 KB subject is what
    the gate actually admits."""
    run = "\\" * (MAX_SCANNABLE_COMMAND_CHARS - 64)
    cmd = "cp " + run + "server\\share\\file.txt /dest"
    assert len(cmd) <= MAX_SCANNABLE_COMMAND_CHARS
    assert security._verb_anchored_sensitive_hit(cmd) is False
    assert _gate_seconds(cmd) < 2.0


def test_pass1_backslash_run_is_linear() -> None:
    """The run never reaches the collapsed loop; the cost was Pass 1 alone.
    Doubling ratio well under 4 (quadratic) with the input sized so the old
    form was already 1.5 s at the smaller size."""
    small = _pattern_seconds(_backslash_run_command(24_000))
    large = _pattern_seconds(_backslash_run_command(96_000))
    assert large < 3.0
    assert large / max(small, 0.005) < 9.0, f"{small:.3f}s -> {large:.3f}s over x4 input"


@pytest.mark.parametrize(
    "make",
    [_double_separator_command, _url_payload_command, _unc_chain_command, _verb_dense_command],
    ids=["double-separator", "url-payload", "unc-chain", "verb-dense"],
)
def test_each_construct_is_linear_in_the_command(make) -> None:  # type: ignore[no-untyped-def]
    """One shape per rewritten construct, sized past the ceiling so the ratio is
    about the algorithm and not the noise. A quadratic term makes x4 input cost
    x16; linear costs x4.

    The RATIO is the assertion here. The absolute bound is a loose backstop
    against a linear-but-absurd constant and is deliberately far from any
    healthy reading: 80-100 KB is above ``MAX_SCANNABLE_COMMAND_CHARS``, so the
    gate refuses a command this long and no caller ever waits for this scan --
    the absolute cost the loop actually pays is pinned at the crash size, under
    the ceiling, by the two tests above. Sized from measurement rather than
    taste: the shape costs ~0.3 s on a dev box, and a four-core CI runner with
    three sibling xdist workers read 4.0 s (Linux) and 5.2 s (Windows) for the
    same work, so a 3 s bound false-reds on the runner while saying nothing
    about the algorithm. A reintroduced quadratic term costs ~20 s here on a dev
    box and a minute on that runner, so the bound still catches one by ~5x.
    """
    small = _pattern_seconds(make(320))
    large = _pattern_seconds(make(1280))
    assert large < 12.0, f"{large:.2f}s on an 80-100 KB command"
    assert large / max(small, 0.02) < 9.0, f"{small:.3f}s -> {large:.3f}s over x4 input"
