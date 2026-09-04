"""The ``explain-for`` skill must stay reachable, and its eval set must stay honest.

Three joints, all of which a prose review of SKILL.md cannot see:

(1) **Reachability.** The skill auto-loads only when one of its ``triggers``
    phrases clears ``_MIN_TRIGGER_OVERLAP`` word-overlap against the user's
    message. A skill whose triggers never fire on the phrasing it was written for
    is dead weight that still costs catalog space, and nothing else in the suite
    checks that. Asserted here through the **real** ``SkillsLoader``, not through
    a reimplementation of its scoring, so the assertion cannot drift from the
    loader it is meant to pin.

(2) **Control prompts.** Word-overlap fires on ordinary phrasing, and this skill
    changes how an answer is written. A prompt that merely contains "explain"
    must NOT pull it in, or every incidental "explain why CI failed" gets pitched
    at a five-year-old. The control cases in ``cases.json`` pin that boundary, so
    loosening a trigger is a test failure rather than a surprise in production.

(3) **Case/skill coherence.** Every case names the audience the skill is expected
    to resolve. If ``cases.json`` names an audience the skill's tables never
    define, the case is measuring nothing. ``run_evals.py --check`` owns that
    rule; this suite runs it so CI enforces it too.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.skills import SkillsLoader

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "explain-for"
SKILL_FILE = SKILL_DIR / "SKILL.md"
EVAL_DIR = ROOT / "evals" / "explain-for"
CASES_FILE = EVAL_DIR / "cases.json"
RUNNER = EVAL_DIR / "run_evals.py"


def _cases() -> list[dict]:
    return json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]


def _case_id(case: dict) -> str:
    return f"{case['id']}-{case['name']}"


@pytest.fixture
def loader(tmp_path: Path) -> SkillsLoader:
    """A loader over a skills tree holding only explain-for.

    Two things are pinned deliberately, and neither is incidental.

    **Only this skill is installed.** With the whole bundled set present,
    ``max_triggered`` could evict explain-for behind an unrelated skill, and the
    reachability assertions would then be measuring trigger competition rather
    than this skill's own triggers.

    **``config`` is passed explicitly, and the fixture is function-scoped.**
    ``SkillsLoader(config=None)`` falls back to ``KiroCrewConfig.load()``, which
    resolves ``config_dir()`` — and that CREATES the operator's real data home as
    a side effect (see ``_isolate_kirocrew_home`` in the rootdir ``conftest.py``),
    which ``no-test-side-effects`` forbids. A module-scoped fixture is built
    BEFORE that function-scoped autouse isolation applies, so the scope is load
    bearing here, not a performance choice.

    Passing the config also decides the verdict rather than inheriting it:
    ``max_triggered`` defaults to **0**, which disables word-overlap trigger
    matching altogether — on a stock install skills are found through the
    Available Skills index and ``skill_search``, not through triggers. So these
    assertions describe the **opt-in** population that has set
    ``max_triggered > 0``; they are not a claim that the skill auto-injects for
    everyone. Read from a real config the value happens to be non-zero on a
    developer box and 0 on a clean CI home, so inheriting it would make the
    assertions pass locally and fail in CI for a reason that has nothing to do
    with the triggers under test. ``1`` is the tightest non-zero cap, so these
    assertions state that explain-for is reachable even when a message may flag
    one skill.
    """
    dest = tmp_path / "skills" / "explain-for"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text(SKILL_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    return SkillsLoader(
        skills_path=tmp_path / "skills",
        install_builtins=False,
        config=KiroCrewConfig(skills=SkillsConfig(max_triggered=1)),
    )


class TestReachability:
    def test_skill_is_discovered(self, loader: SkillsLoader):
        assert "explain-for" in [s["name"] for s in loader.list_skills()]

    def test_declares_triggers(self):
        meta = SkillsLoader._parse_frontmatter(SKILL_FILE)
        assert meta.get("triggers"), "no triggers: the skill can never auto-load"

    @pytest.mark.parametrize("case", _cases(), ids=_case_id)
    def test_trigger_expectation_holds(self, loader: SkillsLoader, case: dict):
        triggered = loader.get_triggered_skills(case["prompt"])
        fired = "explain-for" in triggered
        if case.get("expect_trigger", True):
            assert fired, (
                f"prompt {case['prompt']!r} does not reach explain-for, so the skill "
                "would never load for the phrasing it was written for"
            )
        else:
            assert not fired, (
                f"control prompt {case['prompt']!r} pulls in explain-for; the trigger "
                "set is loose enough to re-pitch an ordinary question at an audience"
            )


class TestCaseCoherence:
    @pytest.mark.parametrize("case", _cases(), ids=_case_id)
    def test_audience_is_defined_by_the_skill(self, case: dict):
        audience = case.get("audience")
        if audience is None:
            assert not case.get("expect_trigger", True), "a triggering case must name an audience"
            return
        body = SKILL_FILE.read_text(encoding="utf-8")
        assert f"| {audience} |" in body, (
            f"audience {audience!r} is not a row in the skill's tables, so the case "
            "grades against a calibration the skill never describes"
        )

    def test_runner_check_mode_passes(self):
        """The eval runner's own deterministic gate, run as a test."""
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
            timeout=120,
        )
        assert proc.returncode == 0, f"run_evals.py --check failed:\n{proc.stdout}\n{proc.stderr}"

    def test_the_duplicate_prompt_rule_has_something_to_compare_against(self):
        """The runner must actually find the skill's worked-shape examples.

        The duplicate-prompt rule compares each case against that parsed list, so
        an empty list does not disable the rule loudly -- it makes it pass every
        case while checking nothing. The parse is coupled to the ``## Worked
        shapes`` heading and the ``**"..."**`` quoting, either of which a later
        edit to the skill could change without touching this harness.
        """
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--check", "-v"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
            timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "would pass vacuously" not in proc.stdout, (
            "the runner parsed no worked-shape examples out of the skill, so the "
            "duplicate-prompt rule is checking nothing"
        )


class TestGradingIsAllOrNothing:
    """A grading counts only if every criterion got an explicit parsed verdict.

    This is the one part of ``--run`` that CI can test, because it is pure. It
    matters because the alternative is silent: back-filling a missing criterion
    as FAIL blames the answer for the GRADER's formatting, and the corrupted
    number lands in the pass rate that ``--run`` exists to produce. A timeout is
    caught one layer up in ``ask``; these are the cases where the grader replied
    and the reply was not usable.
    """

    @staticmethod
    def _load():
        """Reach the script's pure helpers without leaving a trace beside it.

        Two gates constrain this line and their obvious fixes are opposite:
        ``no-test-side-effects`` forbids writing ``__pycache__`` into the
        checkout (which plain ``importlib`` does, and which this repo's
        ``verify_vendor_manifest.py`` gate independently punishes), while
        Semgrep's ``exec-detected`` rule forbids reaching for a dynamic
        compile-and-evaluate to dodge that.

        Neither prescription is applied. The shared requirement is simply that
        the load leave nothing behind, so bytecode writing is suppressed for the
        duration of the import and the flag is restored afterwards. It is
        process-local, and under xdist each worker is its own process, so the
        worst case for a neighbouring import in that window is that it is not
        cached.

        ``test_loading_the_script_leaves_no_bytecode`` asserts the outcome
        rather than trusting this comment: if a later edit drops the guard, a
        test fails instead of a gate quietly filling the tree.
        """
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec = importlib.util.spec_from_file_location("_run_evals_undertest", RUNNER)
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        finally:
            sys.dont_write_bytecode = previous

    def _parse(self, raw: str, count: int):
        return self._load().parse_grading(raw, count)

    def test_loading_the_script_leaves_no_bytecode(self):
        """The guard above must actually hold, not just be documented."""
        cache = RUNNER.parent / "__pycache__"
        before = sorted(p.name for p in cache.glob("*.pyc")) if cache.is_dir() else []
        self._load()
        after = sorted(p.name for p in cache.glob("*.pyc")) if cache.is_dir() else []
        assert after == before, (
            f"loading {RUNNER.name} wrote bytecode into the checkout: "
            f"{sorted(set(after) - set(before))}"
        )

    def test_a_well_formed_reply_parses_every_verdict(self):
        raw = "1|PASS|uses a toy analogy\n2|FAIL|says 'idempotent'\n3|PASS|two sentences"
        got = self._parse(raw, 3)
        assert got == [
            (True, "uses a toy analogy"),
            (False, "says 'idempotent'"),
            (True, "two sentences"),
        ]

    def test_a_reply_missing_one_criterion_is_unusable(self):
        """The failure this closes: criterion 3 would have been recorded FAIL."""
        raw = "1|PASS|fine\n2|PASS|fine"
        assert self._parse(raw, 3) is None

    def test_a_prose_reply_is_unusable_rather_than_all_failures(self):
        raw = "I think the answer is pretty good overall, though a bit long."
        assert self._parse(raw, 4) is None

    def test_an_empty_reply_is_unusable(self):
        assert self._parse("", 2) is None

    def test_renumbered_lines_do_not_silently_shift_verdicts(self):
        """A grader answering 2,3,4 for a 3-criterion case is not a valid grading."""
        raw = "2|PASS|a\n3|PASS|b\n4|PASS|c"
        assert self._parse(raw, 3) is None

    def test_a_verdict_that_is_neither_word_is_not_a_verdict(self):
        """ "unsure" must not read as FAIL -- that is the silent direction.

        A prefix test would count every non-PASS token against the answer, so a
        hedging grader would depress the pass rate with no sign that it had
        hedged rather than judged.
        """
        for token in ("unsure", "N/A", "SKIP", "MAYBE", "-", "FAILED?"):
            raw = f"1|PASS|ok\n2|{token}|hmm"
            assert self._parse(raw, 2) is None, f"{token!r} was accepted as a verdict"

    def test_a_near_miss_of_pass_does_not_read_as_pass(self):
        """ "PASSABLE" starts with PASS but is not PASS."""
        assert self._parse("1|PASSABLE|ok", 1) is None

    def test_verdict_tokens_are_case_and_space_insensitive(self):
        """Only the token identity is strict; casing and padding are not."""
        assert self._parse("1|  pass  |ok\n2|Fail|nope", 2) == [(True, "ok"), (False, "nope")]

    def test_a_repeated_criterion_is_unusable(self):
        """A self-contradicting grader has not judged that criterion.

        The pre-grammar parser assigned by index into a dict, so the LAST line
        for an index silently won and the "every criterion has a verdict" check
        still passed -- the reply looked complete while half of it was
        discarded. Refusing outright is the only reading that does not invent a
        verdict the grader never settled on.
        """
        assert self._parse("1|PASS|good\n1|FAIL|actually bad\n2|PASS|ok", 2) is None

    def test_a_repeated_criterion_is_unusable_even_when_it_agrees(self):
        """Duplication is the defect; agreement between the copies is luck."""
        assert self._parse("1|PASS|good\n1|PASS|good\n2|PASS|ok", 2) is None

    def test_an_index_above_the_criterion_count_is_unusable(self):
        assert self._parse("1|PASS|a\n2|PASS|b\n3|PASS|c", 2) is None

    def test_index_zero_is_unusable(self):
        """Criteria are 1-based, so a 0 means the grader was not counting ours."""
        assert self._parse("0|PASS|a\n1|PASS|b", 1) is None

    def test_prose_alongside_a_full_set_of_verdicts_is_unusable(self):
        """The whole reply is the claim, not the lines that happen to parse.

        A grader that ignores "output nothing else" has already left its
        instructions, so its verdicts stop being trustworthy as verdicts. This
        is the case a reject-known-bad parser cannot reach: every criterion has a
        well-formed verdict, and the reply is still not a grading.
        """
        raw = "Here are my verdicts:\n1|PASS|fine\n2|PASS|fine"
        assert self._parse(raw, 2) is None

    def test_a_trailing_summary_line_is_unusable(self):
        raw = "1|PASS|fine\n2|FAIL|nope\nOverall: 1 of 2 passed."
        assert self._parse(raw, 2) is None

    def test_blank_lines_around_the_verdicts_are_tolerated(self):
        """Blank lines assert nothing, so they are the one thing skipped."""
        raw = "\n1|PASS|ok\n\n2|FAIL|nope\n\n"
        assert self._parse(raw, 2) == [(True, "ok"), (False, "nope")]

    def test_a_reason_containing_a_pipe_is_kept_whole(self):
        """The reason is the rest of the line, not the next field."""
        assert self._parse("1|PASS|uses a|b analogy", 1) == [(True, "uses a|b analogy")]

    def test_an_index_with_a_trailing_dot_still_parses(self):
        assert self._parse("1.|PASS|ok", 1) == [(True, "ok")]


class TestOnlyACleanCallIsAnAnswer:
    """``ask`` returns content only for exit 0 with non-empty stdout.

    Stated as a closed enumeration rather than a list of observed failures: a
    subprocess either never finished, finished badly, or finished with nothing
    to say. Handling them one at a time cost three review rounds on this
    function, so these tests pin the whole gate instead of the last instance.

    Model-free -- the "CLI" here is a Python one-liner, so this runs anywhere.
    """

    @staticmethod
    def _ask(program: str, timeout: int = 30):
        mod_spec = importlib.util.spec_from_file_location("_run_evals_ask", RUNNER)
        assert mod_spec and mod_spec.loader
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            mod = importlib.util.module_from_spec(mod_spec)
            mod_spec.loader.exec_module(mod)
        finally:
            sys.dont_write_bytecode = previous
        return mod.ask([sys.executable, "-c", program], "ignored-prompt", timeout)

    def test_exit_zero_with_output_is_an_answer(self):
        assert self._ask("print('the answer')") == "the answer"

    def test_non_zero_exit_is_not_an_answer_even_with_output(self):
        """The round-3 case: it printed something, so it looked like content."""
        assert self._ask("import sys; print('partial'); sys.exit(1)") is None

    def test_exit_zero_with_empty_output_is_not_an_answer(self):
        assert self._ask("pass") is None

    def test_output_on_stderr_alone_is_not_an_answer(self):
        assert self._ask("import sys; sys.stderr.write('boom')") is None


class TestSkillContent:
    def test_names_the_verbosity_exception(self):
        """The terse levels would otherwise compress the explanation away.

        An agent under answer-only or concise needs the skill to say that an
        explanation request is the exception, or it clips the very output the
        user asked to be expansive.

        The exception is scoped to ONE axis, though. It lifts the ban on
        explaining, not the length bound -- an unscoped "terseness is suspended"
        contradicts answer_only's own length rules, and when two documents
        disagree about length the model takes the longer reading, which is the
        verbosity that mode exists to prevent.
        """
        body = SKILL_FILE.read_text(encoding="utf-8").lower()
        assert "verbosity" in body
        assert "lifts the ban on explaining, not the length bound" in body
        # The register survives every level; length stays with the active one.
        assert "answer_only" in body and "unless the user asked" in body

    def test_requires_ground_truth_before_simplifying(self):
        body = SKILL_FILE.read_text(encoding="utf-8").lower()
        assert "ground truth" in body

    def test_names_the_mechanism_for_colouring_a_diagram(self):
        """Saying "use colour" is not actionable without the mechanism.

        Chat initialises mermaid with a single accent seed plus greys, so a
        diagram is monochrome unless the SOURCE colours it. ``classDef`` is the
        only way an agent can do that, and an agent told to "make it colourful"
        with no mechanism either emits a flat diagram anyway or invents a theme
        directive the host does not honour.
        """
        body = SKILL_FILE.read_text(encoding="utf-8")
        assert "classDef" in body, "no colouring mechanism named"
        assert "A colour is a claim" in body, (
            "the semantic-colour rule is missing, so the guidance reads as "
            "'add colour' and decoration satisfies it"
        )

    def test_offers_widgets_beyond_interaction(self):
        """The widget tier must cover charts and comparisons, not only interaction.

        Scoped to interaction alone it never gets reached for the case it is
        actually best at -- a quantity or a side-by-side that markdown cannot
        express -- so an explanation that wanted a chart gets a paragraph of
        figures instead.
        """
        body = SKILL_FILE.read_text(encoding="utf-8")
        assert "mcwidget" in body
        assert "not only the interaction escape hatch" in body
