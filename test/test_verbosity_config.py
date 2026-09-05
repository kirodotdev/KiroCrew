"""Tests for Response Verbosity (``default`` / ``concise`` / ``ultra`` / ``answer_only``).

Lives under ``test/`` (the collected root per setup.cfg ``testpaths``) so these
run in CI. Covers three layers: the ``{{VERBOSITY_BLOCK}}`` prompt-template
resolution, the dashboard-config PUT/GET validation, and a guard that the
shipped main prompt actually carries the placeholder (so concise mode can never
be silently disabled by a dropped token).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

import kiro_crew
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.context import ContextBuilder


def _resolve(prompt: str, session_key: str, *, verbosity: str = "default") -> str:
    fake_cfg = SimpleNamespace(
        dashboard=SimpleNamespace(widget_density="more", verbosity=verbosity)
    )
    with patch("kiro_crew.context.KiroCrewConfig.load", return_value=fake_cfg):
        return ContextBuilder._resolve_prompt_templates(prompt, session_key)


class TestVerbosityBlockPlaceholder:
    """``{{VERBOSITY_BLOCK}}`` expands on ALL transports when concise; empty on default."""

    def test_default_strips_placeholder_everywhere(self):
        prompt = "prefix {{VERBOSITY_BLOCK}} suffix"
        for key in ("dashboard:abc", "slack:C1:1.2", "cli:local", ""):
            result = _resolve(prompt, key, verbosity="default")
            assert "{{VERBOSITY_BLOCK}}" not in result
            assert "Concise mode is on" not in result

    def test_concise_emits_block_on_every_transport(self):
        for key in ("dashboard:abc", "slack:C1:1.2", "cli:local", ""):
            result = _resolve("{{VERBOSITY_BLOCK}}", key, verbosity="concise")
            assert "## Response Verbosity: Concise" in result
            assert "Lead with the answer" in result

    def test_concise_keeps_safety_carveout(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="concise")
        assert "security warnings" in result
        assert "irreversible" in result
        assert "multi-step" in result

    def test_concise_bounds_the_stakes_carveout_to_omission_not_length(self):
        """The old carve-out ("Ignore concise mode and keep full detail for:
        ...") switched the mode OFF at high stakes — an unbounded length
        licence in the one place the reader most needs the call surfaced, not
        buried. Recast on the same single axis answer_only uses: the warning
        always appears but is one line (call, risk, undoability); an
        order-sensitive multi-step procedure keeps its full length because a
        dropped step IS an omission, and payload was already exempt as
        correctness, not stakes.
        """
        result = " ".join(
            _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="concise").split()
        )
        # The unbounded length licence is gone.
        assert "Ignore concise mode" not in result
        assert "keep full detail" not in result
        # The bounded, omission-focused form is in: the warning must APPEAR,
        # and it is one line.
        assert "Stakes change what concise mode must not omit" in result
        assert "always appear, each as one line naming the call, the risk" in result
        assert "whether it can be undone" in result
        assert "the mechanism and the failure modes are not required" in result

    def test_missing_verbosity_attr_defaults_to_empty(self):
        fake_cfg = SimpleNamespace(dashboard=SimpleNamespace(widget_density="more"))
        with patch("kiro_crew.context.KiroCrewConfig.load", return_value=fake_cfg):
            result = ContextBuilder._resolve_prompt_templates("a {{VERBOSITY_BLOCK}} b", "dashboard:x")
        assert result == "a  b"


class TestUltraConciseBlock:
    """``ultra`` is a distinct, stricter level — not an alias of ``concise``."""

    def test_ultra_emits_its_own_block_on_every_transport(self):
        for key in ("dashboard:abc", "slack:C1:1.2", "cli:local", ""):
            result = _resolve("{{VERBOSITY_BLOCK}}", key, verbosity="ultra")
            assert "## Response Verbosity: Ultra-Brief (ADHD reader)" in result
            assert "simulate the reader" in result
            # The concise block must NOT leak in — the branches are exclusive.
            assert "Concise mode is on" not in result

    def test_ultra_constrains_the_whole_response_not_just_the_opening(self):
        """Regression: the ORIGINAL ultra prompt capped only the opening, then
        said "supporting detail is welcome" and "length after it is fine" —
        which the model read as a licence to expand. Measured output averaged
        1,407 chars, LONGER than default and 76% longer than concise, defeating
        the whole point of the mode. The rewrite removes that licence: the
        suppression must apply to the entire reply, not a lede budget.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "Open with THE answer in 1–2 sentences" in result
        # The expansion licences that caused the bug must be GONE.
        assert "supporting detail is welcome" not in result
        assert "governs the OPENING, not the whole response" not in result
        assert "Length after it is fine" not in result

    def test_ultra_overrides_the_completionist_bias(self):
        """The mechanism that actually shortens output: naming and opposing the
        model's own drive toward completeness, so it stops volunteering detail.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "strong bias toward completeness. Override it" in result
        assert "80% complete in 2 lines beats 100% complete in 20 lines" in result

    def test_ultra_models_the_reader_who_stops_reading(self):
        """Ultra is written for a reader who will not scroll — the prompt must
        say so explicitly, because that framing is what drives prioritization.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "first 2 sentences" in result
        assert "close the tab" in result
        assert "wasted tokens" in result

    def test_ultra_bans_the_structures_that_inflate_output(self):
        """Regression: the original prompt ENCOURAGED tables and structure as
        "signposts", which added tokens instead of removing them. Structure is
        now a banned expansion vector, not an endorsed navigation aid.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "Do NOT add: tables, headers" in result
        assert "would the reader be stuck without this line?" in result
        # The old "structure is not padding" endorsement must be gone.
        assert "it is not padding" not in result

    def test_ultra_caps_supporting_bullets(self):
        """Detail is permitted only when its absence blocks the reader, and is
        bounded — an unbounded bullet list is how the old prompt leaked length.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "only if the reader would be STUCK without them" in result
        assert "Max 3" in result

    def test_ultra_takes_a_position(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "Take a position. Name your pick" in result
        assert 'Resolve "it depends" immediately' in result

    def test_ultra_marks_the_critical_point_for_scanners(self):
        """The reader scans for emphasis before reading — exactly one anchor."""
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "Bold the single most critical point" in result

    def test_ultra_never_cuts_a_required_output_format(self):
        """Regression guard: the brevity rules must not eat a surface-required
        element (an options line, a diff block, a PR URL), which renders the
        response broken rather than terse.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "Required output formats are sacred and never cut" in result
        assert "[OPTIONS:] lines" in result
        assert "diff blocks for file changes" in result
        assert "full PR/MR URLs" in result

    def test_ultra_exempts_explicitly_requested_long_output(self):
        """Brevity constrains UNSOLICITED verbosity — never requested depth."""
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "When the user ASKS for something long" in result
        assert "deliver what was asked" in result

    def test_ultra_is_stricter_than_concise(self):
        ultra = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        concise = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="concise")
        assert ultra != concise
        # concise explicitly ALLOWS a brief progress note; ultra does not.
        assert "Keep progress signal brief, not absent" in concise
        assert "Keep progress signal brief, not absent" not in ultra
        # ultra carries the anti-completionist override; concise does not.
        assert "Override it" in ultra
        assert "Override it" not in concise

    def test_ultra_keeps_safety_carveout(self):
        """The brevity floor: a terse reply must never OMIT a security
        warning, a destructive-action confirmation, or a step in an ordered
        procedure — those failures cause mistakes, not just terseness.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert "security warnings" in result
        assert "irreversible" in result
        assert "multi-step" in result
        # Correctness carve-out: code/errors are never compressed.
        assert "verbatim" in result

    def test_ultra_bounds_the_stakes_carveout_to_omission_not_length(self):
        """The old carve-out ("Never compress for brevity: security warnings,
        ...") was an unbounded length licence: it authorised the model to stay
        verbose exactly at high stakes, the one place ultra's whole framing
        (the reader closes the tab) makes a wall of text most costly. Recast on
        the same single axis answer_only uses — stakes govern what may not be
        OMITTED, never how long the reply is — the warning is mandatory but
        one line; an ordered procedure keeps its full length because a dropped
        step IS an omission, and payload (code, commands, errors) was already
        exempt as correctness, not stakes.
        """
        result = " ".join(
            _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra").split()
        )
        # The unbounded length licence is gone — including its echo in the
        # required-formats bullet, which listed security warnings as a
        # never-cut format ("regardless of brevity").
        assert "Never compress for brevity" not in result
        assert "URLs, security warnings" not in result
        # The bounded, omission-focused form is in: the warning must APPEAR,
        # and it is one line.
        assert "Stakes change what you must not omit, never the length" in result
        assert "always appear, each as one line naming the call, the risk" in result
        assert "whether it can be undone" in result
        assert "the mechanism and the failure modes are not required" in result

    def test_unknown_level_falls_back_to_empty(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="bogus")
        assert result == ""


class TestAnswerOnlyBlock:
    """``answer_only`` is the strictest level: the answer, and no prose around it.

    ``ultra`` still budgets a 1-2 sentence answer plus up to three supporting
    bullets, so it shortens explanation without removing it. ``answer_only``
    removes it: explanation becomes opt-in, capped at one sentence when the
    answer genuinely cannot stand alone.
    """

    def test_answer_only_emits_its_own_block_on_every_transport(self):
        for key in ("dashboard:abc", "slack:C1:1.2", "cli:local", ""):
            result = _resolve("{{VERBOSITY_BLOCK}}", key, verbosity="answer_only")
            assert "## Response Verbosity: Answer Only" in result
            # The other levels must NOT leak in -- the branches are exclusive.
            assert "Concise mode is on" not in result
            assert "Ultra-Brief" not in result

    def test_answer_only_makes_explanation_opt_in(self):
        """The whole point of the level: the user asks, or it does not exist."""
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "Explanation is opt-in" in result
        assert "No explanation by default" in result

    def test_answer_only_caps_unavoidable_context_at_one_sentence(self):
        """A hard numeric cap, because "brief" is what ultra already says and
        the model reads it as a licence to expand. The cap is stated as a
        general rule about reasons rather than a list of cases that earn one:
        the recurring failure is re-deriving a decision already made (chiefly
        justifying an action the model is confident in), and enumerating that
        case as its own bullet would need a new bullet for the next one.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "it is ONE sentence" in result
        assert "never a paragraph" in result
        assert "re-derivation of a decision you have already made" in result

    def test_answer_only_demands_plain_words_not_only_fewer(self):
        """Every other rule here governs LENGTH, so a compliant reply can still
        be four dense, jargon-laden lines -- terse and unreadable. Density is a
        separate axis and needs its own rule.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "Plain words, short sentences" in result
        assert "Brevity is not enough" in result
        assert "jargon that dresses up a simple point" in result

    def test_answer_only_puts_the_point_at_the_front_of_the_sentence(self):
        """Word choice was governed but sentence SHAPE was not, so a reply of
        short plain words could still bury the point mid-sentence behind chained
        clauses ("here", "then", "but", "which means") and contrastive framing
        ("this is not X, it's Y"). The reported symptom was having to hunt for
        what to know. Also fences off the opposite failure: plain is written for
        a capable reader in a hurry, never dumbed down.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        one_line = " ".join(result.split())
        assert "the point at the front of each one" in one_line
        assert "Plain does not mean childish" in one_line
        assert "capable reader in a hurry, not for a five-year-old" in one_line
        assert "in the first few words and stop" in one_line
        assert "here, then, but, so that or which means" in one_line
        assert "read twice to find the point, rewrite it" in one_line

    def test_answer_only_names_the_categories_it_removes(self):
        """Enumerated bans, not a vague "be brief" -- each named category is a
        distinct way explanation creeps back in.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        for banned in (
            "preamble",
            "restating the question",
            "what you just did",
            "rationale",
            "alternatives",
            "caveats",
            "trade-offs",
            "offers to help",
        ):
            assert banned in result, banned
        assert "do not narrate it" in result

    def test_answer_only_cuts_prose_never_payload(self):
        """Regression floor: a mode that removes explanation must not start
        truncating the thing being asked for.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "verbatim and complete" in result
        assert "cuts prose, never payload" in result

    def test_the_payload_carve_out_does_not_cover_quoted_evidence(self):
        """Measured gap this closes: asked "check the logs, what could be
        wrong", answer_only returned a multi-section report -- log excerpts, a
        stack trace, a per-crash timeline, a ruled-out list -- and the user
        could not tell what was broken or what to do. The payload rule was the
        loophole: log text IS an error string and a file's contents, so
        "verbatim and complete, never payload" read as licence to paste every
        line consulted. Payload is scoped to what was ASKED for; quoting to
        prove a point is evidence, which is explanation and therefore opt-in.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "Payload is what the user asked for or has to act on" in result
        assert "Material you quote to prove a point is evidence, not payload" in " ".join(
            result.split()
        )
        assert "evidence is opt-in: leave it out and offer it" in result

    def test_answer_only_bounds_a_halted_or_deviated_task(self):
        """Measured gap this closes: told to fold three things into a PR, the
        model found that main had moved, correctly stopped -- and then wrote
        seven paragraphs justifying the stop (what landed, a quoted docstring,
        the design collision, why its own call was right) before the two
        decisions the user actually had to make. Every other rule frames the
        reply as answering a QUESTION, so a deviation had no answer shape and
        the derivation became the reply. Justifying a deviation feels
        non-optional in a way that explaining an answer does not, so the rule
        has to say the reasoning is opt-in like any other explanation. ORDER is
        the load-bearing half: the user's own manual repair of that reply
        ("what is the suggested action here with simple words") produced an
        imperative first line followed by two sentences of state, so the rule
        names the action as the opener explicitly -- an unordered "state and
        call" still licenses opening on the situation, which is the wall.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "Stopping or deviating is still an answer" in result
        assert "LEAD WITH THE ACTION you recommend" in result
        assert "not with what you found, not with the situation" in " ".join(
            result.split()
        )
        assert "at most two sentences of the state" in result
        assert "Justifying a deviation feels mandatory; it is not" in result

    def test_the_answer_itself_is_bounded_per_item(self):
        """The gap the user was papering over by hand. Every length rule in the
        block governed EXPLANATION -- the one-sentence cap, the cut list, plain
        words -- and nothing bounded the answer, so a verdict plus
        recommendations written as three numbered findings with sub-bullets
        satisfied the whole block. The user ended up appending "use few
        sentences, one sentence for each" to request after request, which is
        the missing rule stated in their own words.

        Bounded PER ITEM on purpose, not as a reply total: a total cap would
        collide with "an ordered multi-step procedure ... stays complete" the
        way ultra's `numbered lists > 3 items` prohibition already does, and
        the invariant here is that no rule governs length and omission at once.
        Per-item scales -- seven steps stay seven steps -- so the two rules are
        orthogonal.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        one_line = " ".join(result.split())
        assert "One sentence per thing you are telling them" in result
        assert "This bounds each item, not the reply" in one_line
        assert "seven one-sentence steps" in one_line
        assert "is a report, and the answer is buried inside it" in one_line

    def test_grounding_is_not_the_answer(self):
        """Third measured shape of the same gap, and the one the payload rule
        could not reach on its own. Asked why a UI fold never fires, the reply
        came back as three numbered findings carrying `gateway.py:6979`, a
        quoted python block, two more `file:line` cites and a leading step
        count -- the verdict (the flag it depends on is never written) was one
        clause inside thirty lines of citation. A code reference is genuinely
        load-bearing for TRUST, which is why the pull toward showing it is
        strong, and the payload clause protects `paths` and `identifiers`
        verbatim, so showing it read as required rather than optional. The rule
        separates the two jobs: grounding is what makes the answer true,
        exposing the grounding is evidence, and evidence is already opt-in.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        one_line = " ".join(result.split())
        assert "Verify against the real thing, then answer without showing the work" in one_line
        assert "only shows that you read it" in result
        assert "Say what the thing does, not where you found it" in result
        assert "hand the reference over when the user asks to check it" in one_line

    def test_the_answer_rule_covers_knowing_not_just_receiving(self):
        """Same measured gap, other half. The rule named three artifact kinds
        (a change, a command, a value), so a question whose answer is a
        JUDGEMENT -- what is wrong, which option, whether it is safe -- matched
        none of them and the model shipped its investigation instead. Every
        other rule was obeyed: no preamble, no rationale, nothing narrated.
        Generalised to what the user needs in order to know or to act, with the
        work that produced it named as explanation, so the rule reaches the
        next question class without enumerating one.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        one_line = " ".join(result.split())
        assert "Whatever the user needs in order to know or to act IS the answer" in one_line
        assert "a verdict" in result
        assert "The work that produced it" in result
        assert "Naming your findings is not naming the answer" in result
        assert "you have not answered" in result

    def test_answer_only_turns_itself_off_when_depth_is_requested(self):
        """Detailed explanations are still reachable -- by asking for depth.
        Without this the level is a dead end rather than a default. The escape
        hatch is scoped to an explicit depth request, NOT to any question that
        contains the word "why" (see the sibling test below).
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        one_line = " ".join(result.split())
        assert "Only an explicit request for depth" in one_line
        assert "this mode is off" in result
        assert "full detail they asked for" in result

    def test_asking_why_does_not_lift_the_length_rules(self):
        """The carve-out used to fire on "asks why" and switch the whole mode
        off, so a bare "why did you override that?" -- a one-line question --
        licensed a full report. The user's own workaround was to append "simple
        sentences to explain" to every why-question, which is the missing bound
        written by hand. A why-question opts into the REASON, not into length:
        the per-item sentence bound and the plain-words rule stay in force.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        one_line = " ".join(result.split())
        assert "A request for the reason is not a request for a document" in one_line
        assert "every length rule stays in force" in one_line
        assert "a few plain sentences, one per point" in one_line
        # The old wholesale flip must be gone, or both readings survive and the
        # model picks the longer one.
        assert "The moment the user asks why" not in one_line

    def test_the_whole_reply_is_pinned_to_explain_for_age_10(self):
        """The bare plain-words rule left the register to taste, and the same
        block also says answer like an expert -- so replies drifted back into
        jargon. The `explain-for` skill already carries a calibrated Age 10 row,
        so the block names it as the register for the WHOLE reply rather than
        re-deriving one, and names it as the default so it is not a per-reply
        judgement call the model can decline.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        one_line = " ".join(result.split())
        assert "Write the WHOLE reply at the `explain-for` skill's Age 10" in one_line
        assert "That Age 10 row is the register for everything this mode emits" in one_line
        assert "not a choice you weigh per reply" in one_line
        # Register and depth are separate axes; conflating the two is how a
        # plain-words rule turns into a licence to write more.
        assert "It sets the REGISTER, never the depth" in one_line
        assert "costs the answer nothing" in one_line

    def test_the_age_10_pin_borrows_calibration_not_length(self):
        """Pointing at another document imports whatever else it says, and
        `explain-for` lifts terseness for explanation requests. Unscoped, the
        two documents disagree about length and the model takes the longer
        reading, which is the exact failure this mode exists to prevent. The pin
        therefore borrows the calibration only and restates that the length
        bound survives -- plus the two things that genuinely outrank it.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        one_line = " ".join(result.split())
        assert "load `explain-for`, follow its Age 10 row" in one_line
        assert "its terseness clause lifts the ban on explaining, not" in one_line
        assert "every length rule above still holds" in one_line
        assert "An audience named in the request wins over Age 10" in one_line

    def test_the_age_10_pin_is_unique_to_answer_only(self):
        """The pin is a property of this tier, not house style. `concise` and
        `ultra` have their own registers, and copying the pin upward would erase
        the distinction between the levels.
        """
        for level in ("concise", "ultra"):
            other = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity=level)
            assert "Age 10" not in other
            assert "explain-for" not in other

    def test_unrequested_explanation_is_the_rare_exception(self):
        """The block previously carried a broad judgement-based licence to
        explain unasked, and the model reached for it constantly -- the reported
        symptom was that answer_only still read verbose. The default is now the
        terse answer plus a one-line offer, and an UNCERTAIN case resolves
        toward omitting, since an unread explanation costs the reader nothing
        to ask for and everything to skip.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "Explaining in full, unasked, is the rare exception" in result
        assert "not a lane you look for" in result
        assert "Assume the user will NOT read an unrequested explanation" in result

    def test_high_stakes_changes_omission_not_length(self):
        """The one contradiction in the earlier block: it demanded mechanism +
        failure modes + reversibility (a paragraph) directly under a
        one-sentence cap, so the two rules disagreed and the longer one won.
        Recast on a single axis -- stakes govern what may not be OMITTED, never
        how long the reply is -- the rules become orthogonal and cannot fight.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "High stakes change what you must NOT omit, never the length" in result
        assert "the mechanism, the failure modes and the reasoning are opt-in" in result

    def test_answer_only_names_the_high_stakes_domains(self):
        """Named domains, so the model does not have to infer what "important"
        means from an abstraction it can rationalise away.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        for domain in (
            "destructive",
            "irreversible",
            "security",
            "credentials",
            "data exposure",
            "permissions",
            "spend",
        ):
            assert domain in result, domain

    def test_high_stakes_warning_leads_with_the_call(self):
        """What the user needs first is the decision, not the derivation: the
        call (or the refusal) plus whether the door swings back. Reasoning that
        arrives before the verdict is not a decision aid, it is a wall the
        verdict is buried in.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "lead with the call" in result
        assert "naming the risk and whether it can be undone" in result
        assert "That single line is the whole warning" in result

    def test_high_stakes_silence_is_named_as_the_failure_not_brevity(self):
        """The failure to guard against is a one-way door handed over without
        mention. Naming brevity as the failure instead is what produced the
        unprompted security essays this level exists to prevent.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "The defect here is silence about a one-way door" in result
        assert "not brevity about it" in result

    def test_the_stakes_hatch_is_unique_to_answer_only(self):
        """All three levels now carry a stakes-govern-omission rule, each in
        its own voice — what stays unique to answer_only is its framing: the
        named failure mode (silence about a one-way door) and the
        whole-warning cap sentence. The discriminators below are fragments
        the other levels genuinely do not carry (their own rules differ by
        more than case), which keeps the levels from converging into copies
        of one block.
        """
        answer_only = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "High stakes change what you must NOT omit" in answer_only
        assert "That single line is the whole warning" in answer_only
        assert "The defect here is silence about a one-way door" in answer_only
        for level in ("concise", "ultra"):
            result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity=level)
            assert "That single line is the whole warning" not in result
            assert "The defect here is silence about a one-way door" not in result

    def test_a_destructive_command_carries_its_undo_path(self):
        """Measured gap this closes: asked how to delete every local branch
        merged into main, answer_only returned the bare command and conveyed
        reversibility in 0/3 samples where unconstrained default managed 2/3
        (two independent graders agreeing). The high-stakes paragraph covers
        RECOMMENDING an action in a consequential class; it did not cover the
        answer simply BEING the destructive command, where "show it and stop"
        applies and stops too early.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "destroys, overwrites or rewrites something" in result
        assert "the undo path rides along with it in the same reply" in " ".join(result.split())
        assert "or plainly that you cannot" in result

    def test_the_undo_note_is_bounded_so_it_cannot_reopen_explanation(self):
        """The rule has to buy exactly one clause. Without a bound it becomes a
        licence to explain, which is the failure mode this whole level exists
        to prevent.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "One clause is enough" in result

    def test_the_undo_rule_is_scoped_to_the_lead_with_it_and_stop_rule(self):
        """It is an exception to stopping, not a new general obligation -- a
        non-destructive command still gets handed over bare.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "One exception to stopping" in result
        assert "Lead with it and stop" in result

    def test_the_undo_rule_names_the_cost_of_omitting_it(self):
        """Naming the consequence is what makes the model treat a missing undo
        path as a defect rather than as successful brevity.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "is not a terse answer, it is a trap" in " ".join(result.split())

    def test_the_undo_rule_is_unique_to_answer_only(self):
        """concise and ultra still permit explanation around a command, so they
        need no such rule; asserting it keeps the levels from converging.
        """
        for level in ("concise", "ultra"):
            assert "One exception to stopping" not in _resolve(
                "{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity=level
            )

    def test_answer_only_keeps_safety_carveout(self):
        """What survives compression unconditionally is narrower than before:
        an ordered procedure (a dropped step causes the mistake) and required
        formats. A risk warning is no longer in this list because it is now
        governed by the one-line high-stakes rule instead -- present always,
        long never.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "ordered multi-step procedure" in result
        assert "a dropped step causes the mistake" in result
        # The risk warning is mandatory but bounded, not exempt from brevity.
        assert "irreversible" in result

    def test_answer_only_never_cuts_a_required_output_format(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "any output format the surface REQUIRES" in result
        assert "[OPTIONS:] lines" in result
        assert "diff blocks for file changes" in result
        assert "full PR/MR URLs" in result

    def test_the_required_format_list_is_illustrative_not_closed(self):
        """The three named formats are only today's set -- per-surface rules and
        steering files add more. A list the model reads as exhaustive silently
        authorises dropping anything unlisted, which is the inverse of intent.
        """
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "illustrative, not exhaustive" in result
        assert "brevity never overrides it" in result

    def test_answer_only_preserves_the_users_language(self):
        result = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        assert "Preserve the user's language" in result

    def test_answer_only_is_stricter_than_ultra(self):
        answer_only = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="answer_only")
        ultra = _resolve("{{VERBOSITY_BLOCK}}", "dashboard:x", verbosity="ultra")
        assert answer_only != ultra
        # ultra budgets an explanation (bullets); answer_only grants none.
        assert "Max 3" in ultra
        assert "Max 3" not in answer_only
        assert "No explanation by default" not in ultra


class TestShippedPromptCarriesToken:
    """Regression guard: the main prompt MUST ship the placeholder, else concise mode is a silent no-op."""

    def test_main_prompt_has_verbosity_placeholder(self):
        prompt_md = Path(kiro_crew.__file__).parent / "config" / "prompt.md"
        assert "{{VERBOSITY_BLOCK}}" in prompt_md.read_text(encoding="utf-8")


class TestVerbosityRoundTrip:
    """dashboard.verbosity persistence (config layer)."""

    @pytest.fixture()
    def cfg_file(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text("{}", encoding="utf-8")
        with patch("kiro_crew.config.loader.config_path", return_value=p):
            yield p

    def test_defaults_to_default(self):
        assert KiroCrewConfig().dashboard.verbosity == "default"

    def test_answer_only_is_an_advertised_enum_value(self):
        """The Settings UI and the config-patch validator both read this enum;
        a level missing here is a level the user cannot select.
        """
        field = KiroCrewConfig().dashboard.__dataclass_fields__["verbosity"]
        assert field.metadata["enum"] == ["default", "concise", "ultra", "answer_only"]

    def test_answer_only_round_trips(self, cfg_file):
        cfg = KiroCrewConfig()
        cfg.dashboard.verbosity = "answer_only"
        cfg.save()
        assert KiroCrewConfig.load().dashboard.verbosity == "answer_only"

    def test_save_load(self, cfg_file):
        cfg = KiroCrewConfig()
        cfg.dashboard.verbosity = "concise"
        cfg.save()
        assert json.loads(cfg_file.read_text())["dashboard"]["verbosity"] == "concise"
        assert KiroCrewConfig.load().dashboard.verbosity == "concise"

    def test_load_from_existing(self, cfg_file):
        cfg_file.write_text(json.dumps({"dashboard": {"verbosity": "concise"}}), encoding="utf-8")
        assert KiroCrewConfig.load().dashboard.verbosity == "concise"


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


@pytest.fixture()
def mock_sel():
    try:
        import kiro_crew.dashboard.handlers  # noqa: F401
    except ImportError:
        pytest.skip("dashboard handler deps not available locally")
    m = MagicMock()
    m.log_tool_invocation = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sel", return_value=m):
        yield m


@pytest.fixture()
def handler_app(cfg_file, mock_sel):
    from kiro_crew.dashboard.handlers.files import api_dashboard_config
    app = web.Application()
    app.router.add_put("/api/dashboard/config", api_dashboard_config)
    app.router.add_get("/api/dashboard/config", api_dashboard_config)
    return as_owner(app)


@pytest.mark.asyncio
async def test_handler_put_verbosity_concise(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"verbosity": "concise"})
        assert resp.status == 200
    assert KiroCrewConfig.load().dashboard.verbosity == "concise"


@pytest.mark.asyncio
async def test_handler_put_verbosity_ultra(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"verbosity": "ultra"})
        assert resp.status == 200
    assert KiroCrewConfig.load().dashboard.verbosity == "ultra"


@pytest.mark.asyncio
async def test_handler_put_verbosity_answer_only(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"verbosity": "answer_only"})
        assert resp.status == 200
    assert KiroCrewConfig.load().dashboard.verbosity == "answer_only"


@pytest.mark.asyncio
async def test_handler_rejection_names_every_accepted_level(handler_app, cfg_file):
    """A 400 that omits a level reads as "that level does not exist"."""
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"verbosity": "aggressive"})
        assert resp.status == 400
        message = (await resp.json())["error"]
    for level in ("default", "concise", "ultra", "answer_only"):
        assert level in message, level


@pytest.mark.asyncio
async def test_handler_put_verbosity_rejects_invalid(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.put("/api/dashboard/config", json={"verbosity": "aggressive"})
        assert resp.status == 400
    # bad value must not be persisted
    assert KiroCrewConfig.load().dashboard.verbosity == "default"


@pytest.mark.asyncio
async def test_handler_get_returns_verbosity(handler_app, cfg_file):
    cfg_file.write_text(json.dumps({"dashboard": {"verbosity": "concise"}}), encoding="utf-8")
    async with TestClient(TestServer(handler_app)) as client:
        resp = await client.get("/api/dashboard/config")
        assert resp.status == 200
        assert (await resp.json())["verbosity"] == "concise"
