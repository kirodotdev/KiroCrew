"""The host-data gate over the ACP frame corpus.

The gate itself is a script so CI can run it without pytest, and these tests hold it
to the two properties that make it a ratchet rather than a linter: the baseline is
per FILE and per MARKER CLASS, and it cannot go stale silently.

A gate whose baseline is a pattern-wide opt-out is worse than no gate, because it
reads as protection. So each test below plants a marker rather than asserting on the
current corpus: a corpus that happens to be clean would make an assertion about it
pass whether the gate works or not.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
GATE_PATH = REPO_ROOT / "scripts" / "check_acp_frame_host_data.py"


def _gate():
    spec = importlib.util.spec_from_file_location("acp_frame_host_data_gate", GATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _gate()


def test_the_shipped_corpus_is_clean_or_baselined(gate) -> None:
    """The gate passes today, so a failure in CI means a NEW leak rather than an old one."""
    failures = gate.sweep(gate.fixture_files(gate.CORPUS))
    assert failures == [], "the corpus carries an unbaselined host marker:\n" + "\n".join(failures)


#: The harness directories whose fixtures predate this gate. The baseline is closed to
#: everything else: a corpus recorded after the gate exists has no reason to owe a debt,
#: and baselining a new one is the single move that would make introducing this
#: pointless. Named here rather than derived from the baseline so ADDING a directory is
#: an edit a reviewer sees.
PRE_GATE_HARNESSES = frozenset({"claude", "codex", "kas", "kiro", "opencode", "pi"})


def test_only_pre_gate_harnesses_carry_debts(gate) -> None:
    """A corpus recorded through the capture script needs no debts.

    The cheapest way to make this gate green over a new fixture is to baseline it, so
    the baseline is held closed to the directories that predate the gate. A harness
    onboarded afterwards -- whose fixtures were pruned as they were recorded -- fails
    here the moment it acquires an entry.
    """
    offenders = sorted(rel for rel in gate.BASELINE if rel.split("/")[3] not in PRE_GATE_HARNESSES)
    assert offenders == [], (
        "these fixtures were recorded after this gate existed, so they must be clean "
        f"rather than baselined: {offenders}"
    )


#: Every (file, marker class) debt the gate carries today, spelled out. Shrink-only is
#: a claim about this set: an entry may LEAVE it (the fixture was re-recorded clean, and
#: the stale-entry check then insists the line goes), and nothing may join it. The
#: directory test above cannot see a new fixture inside a pre-gate directory, or a second
#: class on an existing file, acquiring a line -- this pin can. Removing an entry here is
#: the intended edit when a debt is paid down; adding one is the edit this exists to refuse.
BASELINE_DEBTS = frozenset(
    {
        ("claude/session.expected.json", "a scratch run id"),
        ("claude/session.expected.json", "a run or request uuid"),
        ("claude/session.jsonl", "a scratch run id"),
        ("claude/session.jsonl", "a run or request uuid"),
        ("codex/session-live.expected.json", "a run or request uuid"),
        ("codex/session-live.jsonl", "a run or request uuid"),
        ("codex/session-live.jsonl", "the harness's own command inventory"),
        ("kas/session.expected.json", "a run or request uuid"),
        ("kas/session.jsonl", "a scratch run id"),
        ("kas/session.jsonl", "a run or request uuid"),
        ("kiro/session.expected.json", "a run or request uuid"),
        ("kiro/session.jsonl", "a run or request uuid"),
        ("opencode/permission-request-live.expected.json", "a scratch run id"),
        ("opencode/permission-request-live.jsonl", "a scratch run id"),
        ("opencode/session-live.jsonl", "the harness's own command inventory"),
        ("opencode/session-load-live.jsonl", "the harness's own command inventory"),
        ("opencode/tool-call-live.expected.json", "a scratch run id"),
        ("opencode/tool-call-live.jsonl", "a scratch run id"),
        ("pi/permission-request-live.expected.json", "a run or request uuid"),
        ("pi/permission-request-live.jsonl", "a run or request uuid"),
        ("pi/permission-request-live.jsonl", "the harness's own command inventory"),
        ("pi/permission-request-live.jsonl", "an absolute scratch path"),
        ("pi/session-live.expected.json", "a run or request uuid"),
        ("pi/session-live.jsonl", "a run or request uuid"),
        ("pi/session-live.jsonl", "the harness's own command inventory"),
        ("pi/session-load-live.jsonl", "a run or request uuid"),
        ("pi/session-load-live.jsonl", "the harness's own command inventory"),
    }
)


def test_the_baseline_is_exactly_the_debts_it_carries_today(gate) -> None:
    """Shrink-only, made mechanical: the debt set is pinned entry by entry.

    A new fixture inside a pre-gate directory, or a second marker class on a file that
    already has one, would otherwise acquire a baseline line with no test going red --
    the directory closure above cannot see either. Paying a debt down is the one legal
    edit here: delete the entry from BOTH the gate's baseline and this pin.
    """
    prefix = "test/fixtures/acp_frames/"
    actual = frozenset(
        (rel[len(prefix) :], reason) for rel, reasons in gate.BASELINE.items() for reason in reasons
    )
    assert all(rel.startswith(prefix) for rel in gate.BASELINE)
    added = sorted(actual - BASELINE_DEBTS)
    assert added == [], f"new baseline debt(s) -- re-record the fixture clean instead: {added}"
    retired = sorted(BASELINE_DEBTS - actual)
    assert (
        retired == []
    ), f"debt(s) paid down without updating this pin -- delete them here too: {retired}"


def test_a_new_marker_fails_even_in_a_baselined_file(gate, tmp_path, monkeypatch) -> None:
    """The baseline is per MARKER CLASS, so a second class in the same file still fails.

    The planted file is entered into the baseline under the key the gate itself derives
    for it, which is what makes this a test of the per-REASON rule. Copying a real
    baselined fixture into a temp directory does not: the copy matches no baseline key at
    all, so the gate treats every class as unbaselined and the assertion passes while
    proving only that an unbaselined file fails.
    """
    first = gate.PATTERNS[0][1]
    pattern_second, second = gate.PATTERNS[1][0], gate.PATTERNS[1][1]

    planted = tmp_path / "planted.jsonl"
    planted.write_text(_sample_for(pattern_second) + "\n", encoding="utf-8")
    # Baselined for the FIRST class only, so the second must still be reported.
    monkeypatch.setitem(gate.BASELINE, gate.repo_relative(str(planted)), (first,))

    failures = gate.sweep([str(planted)])
    assert any(second in f for f in failures), (
        f"a {second!r} planted in a file baselined only for {first!r} was not caught: "
        f"{failures}"
    )


def test_a_baselined_class_still_fails_in_another_file(gate, tmp_path, monkeypatch) -> None:
    """The baseline is per FILE, so one file's debt is not a licence for the next.

    Both files are planted and only ONE is baselined, so the pair differs in exactly the
    property under test. Reading the debt off a shipped fixture instead would leave the
    result depending on which markers that fixture happens to carry.
    """
    pattern, reason = gate.PATTERNS[0][0], gate.PATTERNS[0][1]
    sample = _sample_for(pattern)

    forgiven = tmp_path / "forgiven.jsonl"
    forgiven.write_text(sample + "\n", encoding="utf-8")
    monkeypatch.setitem(gate.BASELINE, gate.repo_relative(str(forgiven)), (reason,))

    other = tmp_path / "other.jsonl"
    other.write_text(sample + "\n", encoding="utf-8")

    assert gate.sweep([str(forgiven)]) == [], "a baselined file must not report its own debt"
    failures = gate.sweep([str(other)])
    assert any(
        reason in f for f in failures
    ), f"{reason!r} is baselined on a sibling file, and that silenced it here: {failures}"


def test_a_stale_baseline_entry_is_a_failure(gate) -> None:
    """A debt whose marker is absent from the file is a permission for the next leak."""
    stale = gate._stale_baseline_entries([str(REPO_ROOT / "test" / "fixtures" / "acp_frames")])
    # Every real entry is reported as stale here, because the path list names a
    # DIRECTORY rather than the files -- which is the check working: an entry whose
    # file is not in the swept set cannot be confirmed and is not silently kept.
    assert stale, "a baseline entry whose file was not swept must not pass unnoticed"
    assert all("strike" in line for line in stale), stale


def test_every_baseline_reason_is_a_pattern_this_gate_has(gate) -> None:
    """A misspelled reason would silence nothing and look like it silenced something."""
    known = {why for _pat, why in gate.PATTERNS}
    for rel, reasons in gate.BASELINE.items():
        unknown = [r for r in reasons if r not in known]
        assert unknown == [], f"{rel} baselines {unknown}, which no pattern produces"


def test_a_path_on_another_drive_does_not_crash_the_sweep(gate, tmp_path, monkeypatch) -> None:
    """``os.path.relpath`` RAISES across drives, and a hosted runner hits exactly that.

    A temp directory lands on a different drive from the checkout on Windows CI, so a
    sweep over a planted file raised ``ValueError: path is on mount 'C:', start on mount
    'D:'`` and took the whole test down. The behaviour cannot be reproduced on a POSIX
    host, so the raise is injected rather than waited for -- otherwise this only ever
    fails on the platform least likely to be run locally.
    """
    planted = tmp_path / "planted.jsonl"
    planted.write_text("{}\n", encoding="utf-8")

    def cross_mount(_path, _start=None):
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    monkeypatch.setattr(gate.os.path, "relpath", cross_mount)
    # No entry can exist for a file outside the checkout, so the sweep must report the
    # planted marker rather than raising or silently forgiving it.
    planted.write_text(_sample_for(gate.PATTERNS[0][0]) + "\n", encoding="utf-8")
    failures = gate.sweep([str(planted)])
    assert any(gate.PATTERNS[0][1] in f for f in failures), failures


def test_the_baseline_key_is_posix_spelled_whatever_the_path_separator(gate) -> None:
    """The baseline is authored with ``/`` on every host, so the key must be too.

    On Windows ``relpath`` returns ``os.sep``, so a key built from it missed every
    baseline lookup -- which turned each baselined marker into a fresh failure AND each
    baseline entry into a stale one. Both separators are folded, not just the running
    platform's, so the same path yields the same key on either.
    """
    posix_style = gate.repo_relative(
        gate.os.path.join(
            str(gate.REPO_ROOT), "test", "fixtures", "acp_frames", "kiro", "session.jsonl"
        )
    )
    assert "\\" not in posix_style
    assert posix_style in gate.BASELINE, posix_style

    # A path outside the checkout keeps its own name, normalized, and matches no entry.
    outside = gate.repo_relative("D:\\a\\tmp\\planted.jsonl")
    assert "\\" not in outside
    assert outside not in gate.BASELINE


def test_the_gate_refuses_an_empty_corpus(gate, tmp_path, monkeypatch, capsys) -> None:
    """A gate that passes over nothing is a gate that passes over a deleted corpus."""
    monkeypatch.setattr(gate, "CORPUS", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["check_acp_frame_host_data.py"])
    assert gate.main() == 1
    assert "corpus problem" in capsys.readouterr().out


def _sample_for(pattern: str) -> str:
    """A short string the given gate pattern matches.

    Written per pattern rather than generated, because a generated match would be a
    second implementation of the regexes and would drift from them.
    """
    samples = [
        "/home/someone",
        "/Users/Someone",
        "C:\\\\Users\\\\Someone",
        "runtime-0123abcd",
        "localhost",
        ".venv",
        "0123abcd-0123-0123-0123-0123456789ab",
        "[Internal]",
        "aws_bedrock",
        "activeRunId",
        "availableCommands",
    ]
    for sample in samples:
        if re.search(pattern, sample):
            return sample
    raise AssertionError(f"no sample matches {pattern!r}; add one beside the pattern")
