"""Unit tests for the finding `headline` field and the finding layout it leads.

The reviewer now emits a one-sentence `headline` per finding (see
``review_driver.build_review_task``). Three surfaces consume it — the prompt that
asks for it, the drafted pull-request comment body, and the archived HTML report —
and every one of them has to keep working for a record that predates the field,
because reviews recorded before it exist on disk and are still rendered.
"""
import os
import unittest

from sage_lib import pipeline as PL  # noqa: N812
from sage_lib import report as RP  # noqa: N812
from sage_lib import review_driver as RD  # noqa: N812


def _finding(**over):
    f = {
        "file": "a/b.ts", "line": 42, "severity": "red", "dimension": "scope",
        "headline": "Revoking the blob URL blanks every image already in scrollback.",
        "observation": "cleanup() calls revokeObjectURL on a shared blob.",
        "consequence": "Historic messages lose their images on the next render.",
        "suggestion": "Revoke on unmount, not on effect cleanup.",
        "snippet": "URL.revokeObjectURL(src)",
        "lang": "ts",
    }
    f.update(over)
    return f


class TestPromptAsksForHeadline(unittest.TestCase):
    """The field only exists if the prompt asks for it, so assert on the prompt."""

    def test_review_task_lists_headline_as_a_recorded_finding_field(self):
        task = RD.build_review_task("https://github.com/o/r/pull/1")
        self.assertIn("headline", task)
        # Ordered before `observation` in the field list: the headline is the lead,
        # and a field list that implied otherwise invited the model to restate the
        # observation's first sentence.
        self.assertLess(task.index("dimension, headline"), task.index("observation"))

    def test_review_task_defines_what_a_headline_is(self):
        task = RD.build_review_task("https://github.com/o/r/pull/1")
        # The constraints that make it usable as the one guaranteed-visible line.
        self.assertIn("ONE sentence", task)
        self.assertIn("100", task)
        self.assertIn("no hedging", task)
        self.assertIn("not merely the first sentence of `observation`", task)

    def test_followup_task_also_requires_headline_on_appended_findings(self):
        """The coverage follow-up APPENDS findings to the same record. Without the
        field named here, its findings would render with no lead while the first
        pass's do — a half-populated report from one review."""
        task = RD.build_review_followup_task("https://github.com/o/r/pull/1")
        self.assertIn("headline", task)


class TestCommentBodyLead(unittest.TestCase):
    def test_headline_leads_the_body_in_bold(self):
        body = PL._comment_body(_finding())
        first = body.splitlines()[0]
        self.assertEqual(
            first,
            "🔴 **Revoking the blob URL blanks every image already in scrollback.**")
        # The observation is still there, as its own paragraph below the lead.
        self.assertIn("\n\ncleanup() calls revokeObjectURL on a shared blob.\n", body)

    def test_body_without_headline_is_unchanged(self):
        """A record predating the field must post exactly as it did before: the
        observation leads, with no empty bold wrapper where the headline would be."""
        body = PL._comment_body(_finding(headline=""))
        self.assertEqual(body.splitlines()[0],
                         "🔴 cleanup() calls revokeObjectURL on a shared blob.")
        self.assertNotIn("****", body)

    def test_missing_headline_key_behaves_like_an_empty_one(self):
        f = _finding()
        del f["headline"]
        self.assertEqual(PL._comment_body(f).splitlines()[0],
                         "🔴 cleanup() calls revokeObjectURL on a shared blob.")

    def test_headline_is_redacted_like_every_other_llm_field(self):
        """The headline is model-authored text reaching an external surface, so it
        goes through the same scrub as the rest of the body — it must not be a hole
        opened beside the fields that were already covered."""
        body = PL._comment_body(_finding(
            headline="Token leaked: ghp_0123456789abcdefghijklmnopqrstuvwxyz"))
        self.assertNotIn("ghp_0123456789abcdefghijklmnopqrstuvwxyz", body)


class TestFindingHtml(unittest.TestCase):
    def test_renders_headline_and_labelled_rows(self):
        html = RP._finding_html(_finding())
        self.assertIn("Revoking the blob URL blanks every image already in scrollback.",
                      html)
        # Separation comes from the label column, so the labels must be present.
        for label in ("Observation", "Consequence", "Suggestion", "Evidence"):
            self.assertIn(label, html)
        # The severity word replaces the old coloured dot + dimension pairing.
        self.assertIn("must-fix", html)
        self.assertIn("scope", html)

    def test_should_fix_wording_for_a_yellow_finding(self):
        self.assertIn("should-fix", RP._finding_html(_finding(severity="yellow")))

    def test_dimension_is_escaped_exactly_once(self):
        """The eyebrow is escaped as a whole at the render site, so the dimension
        must reach it RAW. Escaping the part as well double-encodes it and an
        ampersand in a dimension name surfaces as `&amp;amp;` in the report."""
        html = RP._finding_html(_finding(dimension="Correctness & regression"))
        self.assertIn("Correctness &amp; regression", html)
        self.assertNotIn("&amp;amp;", html)

    def test_dimension_is_still_escaped_at_all(self):
        """Raw at the seam is not raw at the output: a dimension carrying markup
        must not become live HTML just because the escape moved outward."""
        html = RP._finding_html(_finding(dimension="<img src=x onerror=alert(1)>"))
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_snippet_stays_preformatted(self):
        """The snippet is code from a private diff. It renders inside a <pre> so it
        is preformatted text to a reader and to assistive tech, not prose that a
        styled div only happens to look like."""
        html = RP._finding_html(_finding())
        self.assertIn("<pre", html)
        self.assertIn("URL.revokeObjectURL(src)", html)

    def test_absent_row_collapses_instead_of_rendering_a_blank_label(self):
        html = RP._finding_html(_finding(consequence="", suggestion=""))
        self.assertNotIn("Consequence", html)
        self.assertNotIn("Suggestion", html)
        self.assertIn("Observation", html)

    def test_record_without_headline_still_renders(self):
        html = RP._finding_html(_finding(headline=""))
        self.assertIn("cleanup() calls revokeObjectURL on a shared blob.", html)
        self.assertIn("must-fix", html)

    def test_headline_is_escaped(self):
        html = RP._finding_html(_finding(headline="<img src=x onerror=alert(1)>"))
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)


class TestSkillSchemaAgreesWithThePrompt(unittest.TestCase):
    """The prompt tells the reviewer to load the `sage-review` skill and follow its
    record schema, so the skill's findings block is a SECOND authoritative field
    list. If the two disagree the reviewer can legitimately follow the skill, omit
    the field, and the card silently renders its fallback — a regression with no
    signal. This test is the guard that keeps them in step."""

    def _skill_text(self) -> str:
        here = os.path.dirname(os.path.dirname(os.path.abspath(RD.__file__)))
        path = os.path.join(here, "skills", "sage-review", "SKILL.md")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_skill_record_schema_lists_headline(self):
        self.assertIn('"headline"', self._skill_text())

    def test_every_finding_field_the_prompt_names_is_in_the_skill_schema(self):
        skill = self._skill_text()
        for field in ("headline", "observation", "consequence", "suggestion",
                      "snippet", "severity", "dimension"):
            with self.subTest(field=field):
                self.assertIn(f'"{field}"', skill)


class TestDesignFacets(unittest.TestCase):
    """A label is only pulled out of a REAL facet line. Prose that merely contains
    an early colon is a sentence, and bolding the clause before it misreads it."""

    def test_newline_split_is_labelled(self):
        lines, labeled = RP._design_facets("Root cause: the URL cache\nFit: reuses Ctx")
        self.assertEqual(len(lines), 2)
        self.assertTrue(labeled)
        self.assertIn("<strong>Root cause:</strong>", RP._facet_html(lines[0], labeled))

    def test_sentence_fallback_is_not_labelled(self):
        blob = ("The API returns 404: the record was already deleted by the sweeper. "
                * 3) + "So the retry is pointless."
        lines, labeled = RP._design_facets(blob)
        self.assertGreater(len(lines), 1)
        self.assertFalse(labeled)
        # The clause before the colon must NOT become a bold label column.
        self.assertNotIn("<strong>", RP._facet_html(lines[0], labeled))


if __name__ == "__main__":
    unittest.main()
