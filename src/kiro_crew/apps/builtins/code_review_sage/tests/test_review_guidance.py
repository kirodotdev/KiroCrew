"""Guard tests: the sage-review ruleset must retain the code-reviewer sub-agent's
explicit strong checks (description<->diff fidelity, security threat chain, API
backward-compat, error handling, observability) as FIRST-CLASS items, not just a
general merge. These lock the gap-closing so a future edit can't silently drop them.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sage_lib.review_driver import build_gate_task  # noqa: E402

_SKILL = (Path(__file__).resolve().parents[1]
          / "skills" / "sage-review" / "SKILL.md")


class TestReviewGuidanceRetainsCodeReviewerChecks(unittest.TestCase):
    text: str

    @classmethod
    def setUpClass(cls):
        cls.text = _SKILL.read_text(encoding="utf-8")

    def test_skill_exists(self):
        self.assertTrue(_SKILL.is_file(), f"missing {_SKILL}")

    def test_strict_bidirectional_description_diff_fidelity(self):
        # Both directions must be explicit: no phantom claim, no undocumented change.
        self.assertIn("STRICT, bidirectional", self.text)
        self.assertIn("phantom", self.text)
        self.assertIn("undocumented", self.text)

    def test_security_threat_chain_is_required(self):
        self.assertIn("threat chain", self.text)
        self.assertIn("trust boundary", self.text)
        self.assertIn("exploit mechanism", self.text)

    def test_api_backward_compat_is_explicit(self):
        self.assertIn("API / contract backward compatibility", self.text)

    def test_error_handling_is_explicit(self):
        self.assertIn("Error-handling comprehensiveness", self.text)

    def test_observability_dimension_present(self):
        self.assertIn("Observability & operability", self.text)

    def test_nine_dimensions_declared(self):
        # The count must stay consistent now that observability is its own dimension.
        self.assertIn("9 code-level dimensions", self.text)
        self.assertIn("9. **Observability", self.text)

    def test_deep_design_reasoning_block_present(self):
        # The design gate must carry a dedicated, deep design-reasoning block so
        # design review (unified with the gate) gets first-class deliberate thought.
        self.assertIn("Deep design reasoning", self.text)
        self.assertIn("Architectural fit", self.text)
        self.assertIn("Contract & data evolution", self.text)
        self.assertIn("Alternatives & proportionality", self.text)
        self.assertIn("Failure modes", self.text)
        self.assertIn("Root cause vs symptom", self.text)

    def test_design_gate_unified_not_separate_stage(self):
        # Design stays unified with the gate — there is no separate "deep dive" stage.
        self.assertIn("unified with the design review", self.text)

    def test_weakest_lens_sets_design_risk(self):
        # The deep-reasoning rule: when lenses conflict, the weakest sets design_risk.
        self.assertIn("weakest", self.text)


class TestGateTaskPromptCarriesDesignLenses(unittest.TestCase):
    prompt: str
    """The driver's Phase-1 gate prompt must instruct deep design reasoning across
    the same lenses as the skill, so the worker actually performs the deep gate."""

    @classmethod
    def setUpClass(cls):
        cls.prompt = build_gate_task("CR-12345678")

    def test_prompt_instructs_deep_thinking(self):
        self.assertIn("THINK DEEPLY", self.prompt)
        self.assertIn("Deep design reasoning", self.prompt)

    def test_prompt_names_the_design_lenses(self):
        for lens in ("architectural fit", "contract/data evolution",
                     "alternatives", "failure modes", "root-cause vs symptom"):
            self.assertIn(lens, self.prompt)

    def test_prompt_keeps_block_design_only(self):
        # Deep reasoning must not weaken the block rule: BLOCK stays design-only.
        self.assertIn("BLOCK is ONLY for a genuine DESIGN defect", self.prompt)


if __name__ == "__main__":
    unittest.main()
