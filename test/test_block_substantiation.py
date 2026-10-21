"""A review lane says PASS or BLOCK, and a BLOCK names a Trigger and an Outcome.

Every lane (the GPT and Opus line reviewers, Design, UX and First Principles, and
the fork copies of all five) carries the same three small shell helpers inline.
They are inline and not sourced because a script sourced from `.github/scripts/`
on a same-repo pull request is the PR's own editable copy, and
``test_ai_review_workflows_description_capture`` forbids running one after a role
is assumed. A copy per lane is the price, so this file pins every copy
byte-identical and runs it.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
from ai_review_workflows_helpers import WORKFLOWS, _bash, _proc_log

BEGIN = "# --- block substantiation (copied into every lane; pinned by test_block_substantiation.py) ---"
END = "# --- end block substantiation ---"

#: lane workflow -> (kind, verdict header prefix or "")
VERDICT_LANES = {
    "design-review.yml": "Design-Verdict:",
    "fork-design-review.yml": "Design-Verdict:",
    "ux-review.yml": "UX-Verdict:",
    "fork-ux-review.yml": "UX-Verdict:",
    "first-principles-review.yml": "First-Principles-Verdict:",
    "fork-first-principles-review.yml": "First-Principles-Verdict:",
}
MARKER_LANES = {
    "codex-review.yml": ("codex-review-output.md", "[GPT-REVIEWED]"),
    "fork-gpt-review.yml": ("codex-review-output.md", "[GPT-REVIEWED]"),
    "claude-review.yml": ("claude-review-output.md", "[OPUS-REVIEWED]"),
    "fork-opus-review.yml": ("claude-review-output.md", "[OPUS-REVIEWED]"),
}

HEAD = "0123456789abcdef0123456789abcdef01234567"


def _read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _helper_block(name: str) -> str:
    """The inline helper block of a lane, dedented, markers included."""
    lines = _read(name).splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    assert len(starts) == 1, f"{name}: expected exactly one substantiation block"
    start = starts[0]
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == END)
    block = lines[start : end + 1]
    indent = len(block[0]) - len(block[0].lstrip())
    return "\n".join(line[indent:] if line.strip() else "" for line in block) + "\n"


class TestEveryLaneCarriesTheSameHelpers:
    def test_verdict_lanes_are_byte_identical(self) -> None:
        blocks = {name: _helper_block(name) for name in VERDICT_LANES}
        reference = blocks["design-review.yml"]
        for name, block in blocks.items():
            assert block == reference, f"{name} drifted from design-review.yml"
        assert "demote_block_verdict() {" in reference
        assert "drop_unsubstantiated_block_marker" not in reference

    def test_marker_lanes_are_byte_identical(self) -> None:
        blocks = {name: _helper_block(name) for name in MARKER_LANES}
        reference = blocks["codex-review.yml"]
        for name, block in blocks.items():
            assert block == reference, f"{name} drifted from codex-review.yml"
        assert "drop_unsubstantiated_block_marker() {" in reference
        assert "demote_block_verdict" not in reference

    def test_both_kinds_share_one_definition_of_a_substantiated_block(self) -> None:
        def definition(block: str) -> str:
            start = block.index("block_substantiated() {")
            return block[start : block.index("\n}\n", start) + 3]

        assert definition(_helper_block("design-review.yml")) == definition(
            _helper_block("codex-review.yml")
        )

    def test_no_lane_sources_a_script_for_it(self) -> None:
        # A sourced copy would be the PR's own editable file on a same-repo PR.
        assert not (WORKFLOWS.parent / "scripts" / "block-substantiation.sh").exists()
        for name in (*VERDICT_LANES, *MARKER_LANES):
            assert "block-substantiation.sh" not in _read(name), name


def _run(script: str, **env: str) -> subprocess.CompletedProcess[str]:
    bash = _bash()
    if bash is None:
        pytest.skip("the lanes are Bash")
    return subprocess.run(
        [bash, "-c", script],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


COMPLETE = textwrap.dedent("""\
    Design-Verdict: BLOCK

    **The probe is read by nobody.**

    ### Blockers
    - The probe ships with zero consumers
      Trigger: a default Windows install starts a session.
      Outcome: every classified spawn fails at boot.
      Clears when: a consumer is named.

    [DESIGN-REVIEWED] abc
    """)
NO_OUTCOME = COMPLETE.replace("  Outcome: every classified spawn fails at boot.\n", "")
EMPTY_TRIGGER = COMPLETE.replace(
    "Trigger: a default Windows install starts a session.", "Trigger:  "
)
BOLD_FIELDS = COMPLETE.replace("Trigger:", "**Trigger:**").replace("Outcome:", "**Outcome:**")


class TestBlockSubstantiated:
    @pytest.mark.parametrize(
        ("body", "ok"),
        [
            (COMPLETE, True),
            (BOLD_FIELDS, True),
            (NO_OUTCOME, False),
            (EMPTY_TRIGGER, False),
            ("Design-Verdict: BLOCK\n\n**Bad design.**\n", False),
            # A field name inside prose is not a field.
            ("Design-Verdict: BLOCK\n\nThe Trigger: and Outcome: are unclear.\n", False),
            ("", False),
        ],
    )
    def test_needs_a_non_empty_trigger_line_and_a_non_empty_outcome_line(
        self, body: str, ok: bool
    ) -> None:
        block = _helper_block("design-review.yml")
        result = _run(block + 'block_substantiated <<< "$BODY"\n', BODY=body)
        assert (result.returncode == 0) is ok, _proc_log(result)

    def test_demote_rewrites_only_the_first_verdict_line(self) -> None:
        block = _helper_block("design-review.yml")
        body = COMPLETE + "\nquoted: design-verdict: BLOCK\n"
        result = _run(block + 'demote_block_verdict "Design-Verdict:" <<< "$BODY"\n', BODY=body)
        assert result.returncode == 0, _proc_log(result)
        lines = result.stdout.rstrip("\n").splitlines()
        assert lines[0] == "Design-Verdict: PASS"
        assert lines[2:] == body.splitlines()[2:]

    def test_demote_leaves_a_body_without_the_header_alone(self) -> None:
        block = _helper_block("design-review.yml")
        result = _run(block + 'demote_block_verdict "Design-Verdict:" <<< "$BODY"\n', BODY="x\ny\n")
        assert result.stdout.rstrip("\n") == "x\ny"


def _marker_body(items: str, marker: bool = True) -> str:
    return (
        "**One issue.**\n\n"
        + items
        + ("[BLOCK-MERGE] " + HEAD + "\n" if marker else "")
        + "[GPT-REVIEWED] "
        + HEAD
        + "\n"
    )


COMPLETE_ITEM = (
    "BLOCKING -- src/a.py:3 -- the guard is gone\n"
    "Trigger: a crafted ref reaches the handler.\n"
    "Outcome: the caller is admitted.\n"
    "Anchor: residual/security\n"
    "Fix: restore it.\n\n"
)
BARE_ITEM = "BLOCKING -- src/a.py:3 -- the guard looks odd\nFix: restore it.\n\n"


class TestDropUnsubstantiatedBlockMarker:
    def _drop(self, tmp_path: Path, body: str) -> tuple[str, str]:
        out = tmp_path / "review.md"
        out.write_text(body, encoding="utf-8")
        block = _helper_block("codex-review.yml")
        result = _run(
            block + f'drop_unsubstantiated_block_marker "{out}" "{HEAD}"\n',
        )
        assert result.returncode == 0, _proc_log(result)
        return result.stdout.strip(), out.read_text(encoding="utf-8")

    def test_a_complete_item_keeps_the_marker(self, tmp_path: Path) -> None:
        said, kept = self._drop(tmp_path, _marker_body(COMPLETE_ITEM))
        assert said == ""
        assert f"[BLOCK-MERGE] {HEAD}" in kept

    def test_a_bare_item_loses_the_marker_and_keeps_the_stamp_and_the_text(
        self, tmp_path: Path
    ) -> None:
        said, kept = self._drop(tmp_path, _marker_body(BARE_ITEM))
        assert said == "dropped"
        assert "[BLOCK-MERGE]" not in kept
        assert f"[GPT-REVIEWED] {HEAD}" in kept
        assert "BLOCKING -- src/a.py:3 -- the guard looks odd" in kept

    def test_one_complete_item_among_bare_ones_keeps_the_marker(self, tmp_path: Path) -> None:
        said, kept = self._drop(tmp_path, _marker_body(BARE_ITEM + COMPLETE_ITEM))
        assert said == ""
        assert f"[BLOCK-MERGE] {HEAD}" in kept

    def test_a_clean_review_is_left_alone(self, tmp_path: Path) -> None:
        said, kept = self._drop(tmp_path, _marker_body("No findings.\n", marker=False))
        assert said == ""
        assert "No findings." in kept

    def test_a_marker_for_another_head_is_not_touched(self, tmp_path: Path) -> None:
        body = _marker_body(BARE_ITEM, marker=False) + "[BLOCK-MERGE] " + "f" * 40 + "\n"
        said, kept = self._drop(tmp_path, body)
        assert said == ""
        assert "[BLOCK-MERGE] " + "f" * 40 in kept


def _verdict_segment(name: str) -> str:
    """The self-contained span of a lane's post step that rules on the verdict."""
    text = _read(name)
    start = text.index('          unsubstantiated=""\n')
    end = text.index('          echo "verdict=$verdict" >> "$GITHUB_OUTPUT"\n', start)
    segment = text[start:end]
    return textwrap.dedent(segment)


class TestTheVerdictSegmentOfEachLane:
    """Run the real span of each lane's post step against a verdict and a body."""

    def _run_segment(
        self, name: str, tmp_path: Path, verdict: str, summary: str
    ) -> tuple[str, str, str]:
        script = (
            f"verdict={verdict!r}\n"
            'summary="$SUMMARY"\n'
            + _verdict_segment(name)
            + 'printf "RESULT-VERDICT:%s\\n" "$verdict"\n'
            + 'printf "RESULT-UNSUBSTANTIATED:%s\\n" "${unsubstantiated:-}"\n'
            + 'printf "RESULT-SUMMARY:\\n%s\\n" "$summary"\n'
        )
        result = _run(script, SUMMARY=summary, HEAD=HEAD, RUNNER_TEMP=str(tmp_path))
        assert result.returncode == 0, _proc_log(result)
        out = result.stdout
        verdict = re.search(r"^RESULT-VERDICT:(.*)$", out, re.M).group(1)
        unsub = re.search(r"^RESULT-UNSUBSTANTIATED:(.*)$", out, re.M).group(1)
        return verdict, unsub, out.split("RESULT-SUMMARY:\n", 1)[1]

    @pytest.mark.parametrize("name", sorted(VERDICT_LANES))
    def test_a_complete_block_stays_a_block(self, name: str, tmp_path: Path) -> None:
        prefix = VERDICT_LANES[name]
        body = COMPLETE.replace("Design-Verdict:", prefix)
        verdict, unsub, summary = self._run_segment(name, tmp_path, "BLOCK", body)
        assert (verdict, unsub) == ("BLOCK", "")
        assert summary.startswith(f"{prefix} BLOCK")

    @pytest.mark.parametrize("name", sorted(VERDICT_LANES))
    def test_a_bare_block_is_published_as_pass(self, name: str, tmp_path: Path) -> None:
        prefix = VERDICT_LANES[name]
        body = NO_OUTCOME.replace("Design-Verdict:", prefix)
        verdict, unsub, summary = self._run_segment(name, tmp_path, "BLOCK", body)
        assert (verdict, unsub) == ("PASS", "1")
        # What a reader of the comment sees is the verdict the gate used.
        assert summary.startswith(f"{prefix} PASS")
        assert f"{prefix} BLOCK" not in summary

    @pytest.mark.parametrize("name", sorted(VERDICT_LANES))
    @pytest.mark.parametrize("word", ["CONCERNS", "PASS - FINE", "MAYBE"])
    def test_a_word_that_is_not_a_verdict_is_no_verdict(
        self, name: str, word: str, tmp_path: Path
    ) -> None:
        verdict, unsub, _ = self._run_segment(name, tmp_path, word, "x\n")
        assert (verdict, unsub) == ("UNKNOWN", "")

    @pytest.mark.parametrize("name", sorted(VERDICT_LANES))
    @pytest.mark.parametrize("word", ["PASS", "UNKNOWN"])
    def test_pass_and_unknown_pass_through(self, name: str, word: str, tmp_path: Path) -> None:
        verdict, unsub, _ = self._run_segment(name, tmp_path, word, "x\n")
        assert (verdict, unsub) == (word, "")

    @pytest.mark.parametrize("name", ["fork-design-review.yml", "fork-first-principles-review.yml"])
    def test_a_fork_lane_rewrites_the_review_file_it_publishes(
        self, name: str, tmp_path: Path
    ) -> None:
        prefix = VERDICT_LANES[name]
        body = NO_OUTCOME.replace("Design-Verdict:", prefix)
        self._run_segment(name, tmp_path, "BLOCK", body)
        written = [p for p in tmp_path.iterdir() if p.name.endswith("-output.md")]
        assert len(written) == 1, [p.name for p in tmp_path.iterdir()]
        assert written[0].read_text(encoding="utf-8").startswith(f"{prefix} PASS")


class TestTheMarkerLanesDropBeforeAnythingReadsTheVerdict:
    @pytest.mark.parametrize("name", sorted(MARKER_LANES))
    def test_the_drop_runs_before_the_verdict_is_assembled(self, name: str) -> None:
        text = _read(name)
        drop = text.index("drop_unsubstantiated_block_marker " + MARKER_LANES[name][0])
        if name.endswith("gpt-review.yml") or name == "codex-review.yml":
            # ahead of the blocking output the adjudication stage is gated on
            assert drop < text.index('echo "blocking=true" >> "$GITHUB_OUTPUT"'), name
        else:
            # ahead of the step that posts the comment and the gate that reads it
            assert drop < text.index("Turn-budget exhaustion"), name

    @pytest.mark.parametrize("name", sorted(MARKER_LANES))
    def test_the_drop_announces_itself(self, name: str) -> None:
        text = _read(name)
        assert "::warning title=Unsubstantiated BLOCK::" in text, name
        assert re.search(r"without a Trigger: line and an Outcome: line", text), name
