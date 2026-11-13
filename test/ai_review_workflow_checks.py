"""Checks the GPT and fork-verdict review-workflow test files share, written once.

Each check lives here as a mixin. A test class in ``test_ai_review_workflows_gpt.py`` or
``test_ai_review_workflows_fork_verdict.py`` takes the mixin as a base, so the node id
stays ``<file>::<Class>::<test>``, the class keeps its own setup, and pytest resolves
the test's fixtures from that file.
"""

from __future__ import annotations

from pathlib import Path


class ProviderEmittedRefusalLineClassifiesAsRefused:
    def test_provider_emitted_refusal_line_classifies_as_refused(self, tmp_path: Path) -> None:
        signature = self._pass_step("GPT 6.1 review (discovery pass)")["env"]["REFUSAL_SIGNATURE"]
        log = f"some progress output\n{signature}.\nLearn more here: https://example.invalid\n"
        assert self._classify(tmp_path, log) == " 1"


class EchoedSignatureInPrControlledTextStaysACrash:
    def test_echoed_signature_in_pr_controlled_text_stays_a_crash(self, tmp_path: Path) -> None:
        # The stream carries the diff the reviewer echoed. A fork PR touching
        # this workflow contains the signature verbatim -- as an indented diff
        # line, never line-leading -- and a crash on such a PR must stay a
        # crash, not reclassify as refused.
        signature = self._pass_step("GPT 6.1 review (discovery pass)")["env"]["REFUSAL_SIGNATURE"]
        log = f'+          REFUSAL_SIGNATURE: "{signature}"\n> quoted: {signature}\n'
        assert self._classify(tmp_path, log) == ""


class SignatureOutsideTheStreamTailStaysACrash:
    def test_signature_outside_the_stream_tail_stays_a_crash(self, tmp_path: Path) -> None:
        # The provider emits the refusal as the stream's final act. A copy of
        # the line early in a long stream (echoed content scrolled past) must
        # not classify a later, unrelated crash.
        signature = self._pass_step("GPT 6.1 review (discovery pass)")["env"]["REFUSAL_SIGNATURE"]
        log = f"{signature}.\n" + ("x" * 80 + "\n") * 100
        assert self._classify(tmp_path, log) == ""


class RefusedPassPublishesItsOwnTerminalState:
    def test_refused_pass_publishes_its_own_terminal_state(self, tmp_path: Path) -> None:
        out, gh_output = self._assemble_verdict(
            tmp_path, failed_passes=" 2", refused_passes=" 2", pass2=None
        )
        assert "Reviewer refused" in out
        # The refusal must not read as a completed review, must not prescribe
        # the re-run that has never been observed to work, and deliberately
        # carries NO machine marker.
        assert "[GPT-REVIEWED]" not in out
        assert "[GPT-REFUSED]" not in out
        assert "Re-run the workflow" not in out
        # The fork lane does resolve /ai-review override gpt $HEAD records, so
        # the refused copy names the same remedy as the same-repo lane.
        assert "/ai-review override gpt" in out
        # Downstream classification rides this output, never a body grep.
        assert "refused=true" in gh_output


class CrashedPassKeepsTheRerunnableIncompleteState:
    def test_crashed_pass_keeps_the_rerunnable_incomplete_state(self, tmp_path: Path) -> None:
        out, gh_output = self._assemble_verdict(
            tmp_path, failed_passes=" 1", refused_passes=None, pass2=None
        )
        assert "Incomplete review" in out
        assert "Re-run the workflow" in out
        assert "[GPT-REFUSED]" not in out
        assert "refused=false" in gh_output


class RefusalDominatesAMixedFailure:
    def test_refusal_dominates_a_mixed_failure(self, tmp_path: Path) -> None:
        # Pass 1 crashed AND pass 2 was refused: the adjudication route is the
        # reliable exit either way, while a re-run only helps if the refusal
        # does not recur, so the refusal is what the operator is told about.
        out, gh_output = self._assemble_verdict(
            tmp_path, failed_passes=" 1 2", refused_passes=" 2", pass2=None
        )
        assert "Reviewer refused" in out
        assert "Re-run the workflow" not in out
        assert "refused=true" in gh_output


class CleanRunStillPublishesPass2Verbatim:
    def test_clean_run_still_publishes_pass_2_verbatim(self, tmp_path: Path) -> None:
        verdict = f"all good\n[GPT-REVIEWED] {self.HEAD}\n"
        out, gh_output = self._assemble_verdict(
            tmp_path, failed_passes="", refused_passes=None, pass2=verdict
        )
        assert f"[GPT-REVIEWED] {self.HEAD}" in out
        assert "[GPT-REFUSED]" not in out
        assert "refused=false" in gh_output
