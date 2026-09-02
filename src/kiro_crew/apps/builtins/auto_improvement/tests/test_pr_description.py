"""CR title + commit-message + description authoring (perf and bug tracks).

`spine/pr_description.py` turns a measurement into the human-facing text a reviewer reads —
the CR title, the kept-commit message, and the CR body. It is pure string formatting with
no I/O, and its job is precisely to NOT read like a raw seed dump: it strips lint-code
prefixes, leads a bug title with the user-visible symptom, and renders the measured win as
the headline. Those transformations are exactly the kind that rot silently, so they are
pinned here.

The reward-hacking-guards bullet is a REQUIRED CR section (a perf win that shrank capability
is not a win), so the perf body is checked to surface the RH verdict.
"""

from __future__ import annotations

from kiro_crew.apps.builtins.auto_improvement.spine import pr_description as D
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    TRACK_BUG,
    TRACK_PERF,
    BugGateResult,
    BugReproducingTest,
    Candidate,
    Measurement,
    Proposal,
    StageBreakdown,
)


def _perf_proposal(desc: str = "enable the warm pool") -> Proposal:
    return Proposal(
        cand_id="c1",
        candidate=Candidate(kind=TRACK_PERF, target="src/pool.py::warm", signature=desc),
        worktree=None,  # type: ignore[arg-type]
        branch="main",
        description=desc,
        diff="",
    )


def _verify(delta: float, **kw) -> Measurement:
    return Measurement(ok=True, primary_delta=delta, noise_band=1.0, **kw)


class TestTitleHelpers:
    def test_a_lint_code_prefix_is_stripped_from_a_phrase(self) -> None:
        """The code belongs in the body's evidence, not the human-facing title."""
        assert D._clean_phrase("DTZ011: naive datetime used") == "naive datetime used"
        assert D._clean_phrase("B905: zip without strict") == "zip without strict"

    def test_a_long_phrase_is_cut_on_a_word_boundary_with_an_ellipsis(self) -> None:
        out = D._clean_phrase("alpha beta gamma delta epsilon zeta", limit=20)
        assert out.endswith("…")
        assert " " not in out[-2:], "should cut on a word boundary, not mid-word"
        assert len(out) <= 21

    def test_one_line_collapses_newlines_so_a_subject_cannot_split(self) -> None:
        """An embedded newline must never split a commit subject or inject a 2nd heading."""
        assert "\n" not in D._one_line("line one\nline two\n\nthree", limit=100)

    def test_fmt_is_stable_and_handles_none(self) -> None:
        assert D._fmt(None) == "?"
        assert D._fmt(-2835.9512, nd=1) == "-2836.0"  # standard round-half rounding

    def test_perf_title_leads_with_the_lever_then_the_measured_win(self) -> None:
        title = D.perf_cr_title(
            _perf_proposal(), _verify(-2835.95), primary_name="ttft_ms", unit="ms"
        )
        assert title.startswith("perf: enable the warm pool")
        assert "-2835.9ms" in title and "ttft_ms" in title

    def test_perf_title_marks_a_positive_delta_with_a_plus(self) -> None:
        """A positive delta on a lower-is-better metric is a REGRESSION; the sign must show
        so a reviewer is not misled into reading it as a win."""
        title = D.perf_cr_title(_perf_proposal(), _verify(+5.0), primary_name="ttft", unit="ms")
        assert "+5.0ms" in title

    def test_perf_title_without_a_delta_degrades_to_the_lever_only(self) -> None:
        title = D.perf_cr_title(
            _perf_proposal(), Measurement(ok=False), primary_name="ttft", unit="ms"
        )
        assert title == "perf: enable the warm pool"

    def test_bug_title_leads_with_the_symptom_and_appends_the_file(self) -> None:
        cand = Candidate(
            kind=TRACK_BUG,
            target="src/cron_handler.py::run",
            signature="DTZ011: datetime.date.today() used",
            severity_note="user-visible: scheduled standup reports the wrong day",
        )
        title = D.bug_cr_title(cand)
        assert title == "fix: scheduled standup reports the wrong day (cron_handler.py)"

    def test_bug_title_falls_back_to_the_signature_on_a_placeholder_symptom(self) -> None:
        """A generic 'candidate defect' placeholder is not a symptom — prefer the signature."""
        cand = Candidate(
            kind=TRACK_BUG,
            target="m.py::f",
            signature="off-by-one in the retry counter",
            severity_note="static-analysis (B012) candidate defect",
        )
        assert D.bug_cr_title(cand) == "fix: off-by-one in the retry counter (m.py)"


class TestStageAndGuardrailLines:
    def test_empty_channels_say_so_rather_than_render_blank(self) -> None:
        assert "no per-stage" in D._stage_line({})
        assert "no guardrails" in D._guardrail_line({}, {})

    def test_a_guardrail_within_tolerance_is_marked_within_tol(self) -> None:
        line = D._guardrail_line({"rss": 1.0}, {"rss": 2.0})
        assert "within tol" in line and "rss=1.000" in line

    def test_a_guardrail_over_tolerance_is_marked_OVER(self) -> None:
        line = D._guardrail_line({"rss": 5.0}, {"rss": 2.0})
        assert "OVER tol" in line

    def test_the_rh_line_shouts_when_a_guard_tripped(self) -> None:
        ok = D._rh_guard_line(Measurement(ok=True, rh_capability_ok=True, rh_functional_ok=True))
        assert "no silent shrink" in ok and "passed" in ok
        bad = D._rh_guard_line(Measurement(ok=True, rh_capability_ok=False, rh_functional_ok=False))
        assert "CAPABILITY SHRANK" in bad and "FUNCTIONAL PROBE FAILED" in bad


class TestPerfMessageBuilders:
    def test_the_commit_message_headline_is_the_measured_win(self) -> None:
        msg = D.perf_commit_message(
            proposal=_perf_proposal(),
            verify=_verify(-100.0, stages=StageBreakdown(stages={"io": -80.0})),
            reproduce=_verify(-98.0),
            cycle=3,
            primary_name="ttft_ms",
            unit="ms",
            diff_ref="c3.diff",
        )
        # The commit-message subject uses the "auto:" prefix (distinct from the CR
        # TITLE's "perf:" — the commit is machine-authored, the title is reviewer-facing).
        assert msg.startswith("auto:")
        assert "ttft_ms" in msg

    def test_the_pr_description_surfaces_the_reward_hack_guard_section(self) -> None:
        body = D.perf_pr_description(
            proposal=_perf_proposal(),
            verify=_verify(-100.0, rh_capability_ok=True, rh_functional_ok=True),
            reproduce=_verify(-98.0),
            cycle=1,
            base_anchor="main @ deadbeef",
            fingerprint="fp1",
            primary_name="ttft_ms",
            unit="ms",
            diff_ref="c1.diff",
        )
        assert "no silent shrink" in body, "the required RH-guard section is missing"

    def test_an_unproven_ruler_is_disclosed_in_the_body(self) -> None:
        """A perf PR claims "Evidence it's a real win" — say so when the ruler is unproven.

        In advisory mode a canary that did not clear the band still lets the run proceed
        (the reference profile's canary is a forced LOWER BOUND, not a genuine known win),
        and the keeper's accept test is unaffected. But the reader deciding in minutes
        cannot tell "band proven to resolve this" from "band is a floor" — and only the
        body can tell them. Raised by the GPT review of this branch.
        """
        kwargs = dict(
            proposal=_perf_proposal(),
            verify=_verify(-100.0),
            reproduce=_verify(-98.0),
            cycle=1,
            base_anchor="main @ deadbeef",
            fingerprint="fp1",
            primary_name="ttft_ms",
            unit="ms",
            diff_ref="c1.diff",
        )
        unproven = D.perf_pr_description(**kwargs, ruler_proven=False)  # type: ignore[arg-type]
        assert "Ruler not proven on this target" in unproven
        # The measured claim must survive the caveat, not be replaced by it.
        assert "ttft_ms" in unproven and "REPRODUCE" in unproven

        # Default and explicit-True stay silent: crying wolf on a proven ruler would train
        # reviewers to ignore the warning.
        assert "Ruler not proven" not in D.perf_pr_description(**kwargs)  # type: ignore[arg-type]
        assert "Ruler not proven" not in D.perf_pr_description(
            **kwargs, ruler_proven=True  # type: ignore[arg-type]
        )

    def test_the_driver_reports_the_canary_verdict_to_the_pipeline(self) -> None:
        """The flag is useless unless something real sets it from the preflight result."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        src = inspect.getsource(Driver.preflight)
        assert "ruler_proven" in src and "canary_cleared" in src


class TestBugMessageBuilders:
    @staticmethod
    def _bug_proposal() -> Proposal:
        cand = Candidate(
            kind=TRACK_BUG,
            target="src/x.py::f",
            severity_note="user-visible: errors look like empty replies",
            reproducing_test=BugReproducingTest(test_id="test/test_x.py::test_err"),
        )
        return Proposal(
            cand_id="b1",
            candidate=cand,
            worktree=None,  # type: ignore[arg-type]
            branch="main",
            description="return the error frame",
            diff="",
        )

    def test_the_bug_commit_message_names_the_reproducing_test(self) -> None:
        msg = D.bug_commit_message(
            proposal=self._bug_proposal(),
            bug_res=BugGateResult(passed=True, red=True, green=True, staygreen=True),
            cycle=2,
            diff_ref="b1.diff",
        )
        assert msg.startswith("fix:")
        assert "test/test_x.py::test_err" in msg

    def test_the_bug_description_states_the_red_green_narrative(self) -> None:
        body = D.bug_pr_description(
            proposal=self._bug_proposal(),
            bug_res=BugGateResult(
                passed=True, red=True, green=True, staygreen=True, build_ok=True, collected=True
            ),
            cycle=2,
            base_anchor="main @ deadbeef",
            fingerprint="fp-bug",
            diff_ref="b1.diff",
        )
        # The correctness narrative reviewers rely on.
        assert "test/test_x.py::test_err" in body


class TestDispatch:
    def test_commit_message_routes_by_track(self) -> None:
        """One call-site, two tracks: the dispatcher must pick the right author by kind."""
        perf = D.commit_message(
            proposal=_perf_proposal(),
            cycle=1,
            diff_ref="c1.diff",
            verify=_verify(-10.0),
            reproduce=_verify(-9.0),
            primary_name="ttft",
            unit="ms",
        )
        assert perf.startswith("auto:")

        bug = D.commit_message(
            proposal=TestBugMessageBuilders._bug_proposal(),
            cycle=1,
            diff_ref="b1.diff",
            bug_res=BugGateResult(passed=True, red=True, green=True, staygreen=True),
        )
        assert bug.startswith("fix:")
