"""CR description + commit-message authoring (spine, milestone M5).

Deterministic authors for the two human-readable artifacts a kept, reproduced finding
produces (06_cr_generation_and_dedup.md §3):

  1. the **kept commit message** — committed locally on the working branch in the
     push-disabled clone (reader: ``git log``); convention from ARCHITECTURE.md §2.4
     (06_*.md §3.1).
  2. the **CR description** (``pr_queue/<fp>.cr.md``) — the body of the draft review
     (reader: the human who reviews/publishes/merges in the morning); the §3.2 skeleton.

CRITICAL invariant (06_*.md §3.2 end / §1.2): both artifacts are **derived from the same
measured numbers** the deterministic harness produced — never from a model's recollection.
So this module is *pure, deterministic Python* that renders the measured :class:`Measurement`
(perf) or :class:`BugGateResult` (bug) into prose. The *model* (``claude -p``) may later
expand the prose body, but the spine always has a complete, attributable description on its
own; drafting never blocks on a model call (06_*.md §3.2 "claude -p writes the description"
is an enrichment, not a gate — the spine renders the evidence).

Target-agnostic (10_roadmap M5 generalization note; M0/M1 exit grep): the field *names*
this renders (``primary_name``, the substage keys, the guardrail keys, the RH-guard labels)
all come off the profile's ruler / the measured payload — the spine hard-codes no metric
name, no build tool, no provider token. The §3.1 table's "generalized contract is the
*shape*, with profile-supplied fields" is realized exactly here: one shape, profile fields.

Docs: 06_cr_generation_and_dedup.md §3.1 (commit msg), §3.2 (CR skeleton), §3.3 (RH-guard
section), §4 (draft/--no-open), §4.2 (bug correctness narrative); ARCHITECTURE.md §2.4.
"""

from __future__ import annotations

import re

from .contracts import TRACK_BUG, BugGateResult, Candidate, Measurement, Proposal


def _fmt(x: float | None, *, nd: int = 3) -> str:
    """Render a measured number stably (no locale, fixed precision) so the commit
    message and the CR description never disagree on a value (06_*.md §3.2 end)."""
    if x is None:
        return "?"
    return f"{x:.{nd}f}"


def _one_line(s: str, *, limit: int) -> str:
    """Collapse all whitespace (incl. newlines) to single spaces and cap length, so
    an embedded newline can never split a git commit subject across lines or inject a
    second markdown heading after the title (the first line must stay the subject/heading)."""
    return " ".join((s or "").split())[:limit]


def _clean_phrase(s: str, *, limit: int = 72) -> str:
    """Tidy a seed/hypothesis string into a title phrase: collapse whitespace, drop a
    leading lint-code prefix (``DTZ011: ...`` → ``...``) and trailing punctuation, and
    cap the length on a word boundary so the title reads like a human wrote it."""
    s = " ".join((s or "").split())
    # Strip a leading "CODE: " lint prefix (e.g. "DTZ011: ", "B905: ") — the code goes in
    # the body's evidence section, not the human-facing title.

    s = re.sub(r"^[A-Z]{1,6}[0-9]{2,4}\s*[:\-]\s*", "", s)
    s = s.rstrip(" .")
    if len(s) > limit:
        cut = s[:limit].rsplit(" ", 1)[0]
        s = (cut or s[:limit]).rstrip(" .,;:") + "…"
    return s


def perf_cr_title(proposal: Proposal, verify: Measurement, *, primary_name: str, unit: str) -> str:
    """A human-readable perf CR title: the lever, then the measured win. Reads like
    ``perf: enable the warm pool — ttft_delta_ms −2835.95ms`` instead of a raw seed dump
    (task #22). The measured delta is the headline reviewers care about."""
    lever = _clean_phrase(
        proposal.description or getattr(proposal.candidate, "signature", ""), limit=60
    )
    delta = verify.primary_delta if verify is not None else None
    if delta is not None:
        sign = "" if delta < 0 else "+"  # negative = improvement on lower-is-better
        return f"perf: {lever} — {sign}{_fmt(delta, nd=1)}{unit} {primary_name}"
    return f"perf: {lever}"


def bug_cr_title(candidate: Candidate) -> str:
    """A human-readable bug-fix CR title: lead with the user-visible symptom (the
    ``severity_note``) when present, else the cleaned signature; append the file the fix
    touches. Reads like ``fix: scheduled standup reports the wrong day (cron_handler.py)``
    instead of ``fix: DTZ011: datetime.date.today() used`` (task #22)."""
    symptom = getattr(candidate, "severity_note", "") or ""
    # severity_note is often "user-visible: <symptom>" — strip the label.

    symptom = re.sub(r"^\s*user[- ]visible\s*[:\-]\s*", "", symptom, flags=re.I).strip()
    # The discovery seeds sometimes set severity_note to a generic "static-analysis (RULE)
    # candidate defect" placeholder — prefer the signature in that case.
    if not symptom or "candidate defect" in symptom.lower():
        phrase = _clean_phrase(candidate.signature or candidate.hypothesis, limit=64)
    else:
        phrase = _clean_phrase(symptom, limit=64)
    # Append the touched file basename for at-a-glance scope (target is "<path>::<symbol>").
    loc = (candidate.target or "").split("::", 1)[0].rsplit("/", 1)[-1]
    return f"fix: {phrase}" + (f" ({loc})" if loc else "")


def _stage_line(stages: dict[str, float]) -> str:
    """The attributable sub-stage line — proves the win is where the hypothesis
    claimed (06_*.md §3.1 ``stages:`` row, §3.2 "which sub-stage moved")."""
    if not stages:
        return "(no per-stage breakdown reported)"
    return "  ".join(f"{k}={_fmt(v)}" for k, v in sorted(stages.items()))


def _guardrail_line(guardrails: dict[str, float], tolerances: dict[str, float]) -> str:
    """Each guardrail Δ vs best + its tolerance (06_*.md §3.1 ``guardrails:`` row,
    §3.2 "each guardrail Δ vs best, within tolerance"). A guardrail value is a
    regression magnitude; a value within tolerance is reported as ``(within tol)``."""
    if not guardrails:
        return "(no guardrails reported)"
    parts = []
    for k, v in sorted(guardrails.items()):
        tol = tolerances.get(k, 0.0)
        ok = "within tol" if v <= tol else "OVER tol"
        parts.append(f"{k}={_fmt(v)} (tol {_fmt(tol)}, {ok})")
    return "  ".join(parts)


def _rh_guard_line(meas: Measurement) -> str:
    """The reward-hacking-guards bullet — a REQUIRED CR section (06_*.md §3.3): a perf
    win that shrinks capability is reward hacking, not a win. Surface that it was checked
    so the reviewer sees it (RH-A no capability shrink; RH-B functional probe passed)."""
    cap = (
        "capability set unchanged (no silent shrink)"
        if meas.rh_capability_ok
        else "CAPABILITY SHRANK"
    )
    func = "functional probe passed" if meas.rh_functional_ok else "FUNCTIONAL PROBE FAILED"
    return f"{cap}; {func}"


# ── perf-track commit message (06_*.md §3.1; ARCHITECTURE.md §2.4 shape) ─────


def perf_commit_message(
    *,
    proposal: Proposal,
    verify: Measurement,
    reproduce: Measurement,
    cycle: int,
    primary_name: str,
    unit: str,
    diff_ref: str,
    guardrail_tolerances: dict[str, float] | None = None,
) -> str:
    """Render the kept-commit message in the §2.4 *shape* with profile-supplied fields.

    The §2.4 template is Backend-TTFT-specific in its field NAMES; the spine enforces the
    SHAPE (headline / primary delta line / attributable sub-stages / guardrails / tests /
    held-out / diff-ref) and fills it from the measured payload + the profile's
    ``primary_name``/``unit`` (06_*.md §3.1 generalized-contract table). Every kept commit
    is independently reviewable and could be cherry-picked into a real CR (§3.1 / §2.4)."""
    tol = guardrail_tolerances or {}
    delta = verify.primary_delta
    headline = (
        f"auto: {_one_line(proposal.description, limit=60)}  "
        f"[{_fmt(delta)}{unit} {primary_name}]"
    )
    return "\n".join(
        [
            headline,
            "",
            f"cycle: {cycle}   candidate: {proposal.cand_id}",
            f"primary {primary_name}: {_fmt(verify.primary_base)} -> {_fmt(verify.primary_cand)} "
            f"(Δ {_fmt(delta)}, band ±{_fmt(verify.noise_band)})",
            f"stages: {_stage_line(verify.stages.stages)}   ({unit}, median)",
            f"guardrails: {_guardrail_line(verify.guardrails, tol)}",
            f"reproduce: independent A/B Δ {_fmt(reproduce.primary_delta)} "
            f"(same direction, beats band again)",
            f"reward-hacking guards: {_rh_guard_line(verify)}",
            "tests: PASS (gate green)",
            f"diff-ref: {diff_ref}",
        ]
    )


# ── perf-track CR description (06_*.md §3.2 skeleton) ─────────────────────────


def perf_pr_description(
    *,
    proposal: Proposal,
    verify: Measurement,
    reproduce: Measurement,
    cycle: int,
    base_anchor: str,
    fingerprint: str,
    primary_name: str,
    unit: str,
    diff_ref: str,
    guardrail_tolerances: dict[str, float] | None = None,
    ruler_proven: bool = True,
) -> str:
    """Author the full attributable CR description for a verified+reproduced perf win.

    Implements the 06_*.md §3.2 markdown skeleton verbatim in section structure — What &
    why / Evidence (VERIFY + REPRODUCE + stages + guardrails + RH guards) / Correctness /
    Diff / Provenance — so a reviewer can decide in minutes. Derived from the SAME measured
    numbers as :func:`perf_commit_message` (§3.2 end: commit and CR never disagree)."""
    tol = guardrail_tolerances or {}
    cand = proposal.candidate
    # Disclose an UNPROVEN ruler in the body. Both A/B deltas below still had to clear the
    # calibrated band — that accept test is independent of the canary — but when the
    # Phase-1 canary did not clear it, the band is a demonstrated LOWER BOUND on
    # sensitivity rather than proof the ruler resolves a small win. A human deciding in
    # minutes should be told which of those two they are reading, and only the body can
    # tell them. Raised by the GPT review of this branch.
    caveat = (
        ""
        if ruler_proven
        else (
            "\n> **Ruler not proven on this target.** The Phase-1 canary did not clear the "
            "calibrated band, so the band is a lower bound on sensitivity rather than proof "
            "the ruler resolves a win this small. The two A/B measurements below still had "
            "to beat that band. Treat the magnitude as indicative and the direction as the "
            "claim; configuring a real `benchmarkCommand` workload is what makes this "
            "provable.\n"
        )
    )
    return f"""# auto: {_one_line(proposal.description, limit=70)}
{caveat}

## What changed & why
{cand.hypothesis or proposal.description}

Hot path / surface: `{cand.target}` (scenario: {cand.scenario or 'n/a'}).
Mechanism: {cand.signature or 'see diff'}.

## Evidence it's a real win (verified + reproduced)
- VERIFY:    primary {primary_name} Δ {_fmt(verify.primary_delta)}{unit} vs current best \
({_fmt(verify.primary_base)} -> {_fmt(verify.primary_cand)}{unit}; band ±{_fmt(verify.noise_band)})
- REPRODUCE: second independent A/B Δ {_fmt(reproduce.primary_delta)}{unit} \
(same direction, beats the band again — first-run fluke ruled out)
- stages/attribution: {_stage_line(verify.stages.stages)}
- guardrails: {_guardrail_line(verify.guardrails, tol)}
- reward-hacking guards: {_rh_guard_line(verify)}

## Correctness
- gate: build/imports GREEN
- tests: PASS (gate green)

## Diff
- diff-ref: {diff_ref}   (also attached as the review revision)

## Provenance
- kind: {cand.kind}   base: {base_anchor}   cycle: {cycle}   fingerprint: {fingerprint}
"""


# ── bug-track commit message + CR description (06_*.md §3.1 bug row, §4.2) ────


def bug_commit_message(
    *, proposal: Proposal, bug_res: BugGateResult, cycle: int, diff_ref: str
) -> str:
    """Render the kept-commit message for a bug fix in the §3.1 shape, bug-track instance.

    Headline = ``fixes <repro-test name>``; the "primary delta line" becomes the RED→GREEN
    transition; the "guardrails" row becomes suite-stays-green; there are no A/B reps
    (06_*.md §3.1 generalized-contract table, "Bug-track instance" column)."""
    cand = proposal.candidate
    rt = cand.reproducing_test
    test_id = rt.test_id if rt else "(no test)"
    return "\n".join(
        [
            f"fix: {_one_line(cand.signature or proposal.description, limit=60)}  [fixes {test_id}]",
            "",
            f"cycle: {cycle}   candidate: {proposal.cand_id}",
            "RED→GREEN: test failed@base (x2 flake check) -> passes@fix",
            f"suite-stays-green: {bug_res.staygreen} "
            f"({'no regressions' if not bug_res.failing_tests else ', '.join(bug_res.failing_tests[:5])})",
            f"static-triage: build_ok={bug_res.build_ok} lint_ok={bug_res.lint_ok} "
            f"collected={bug_res.collected}",
            f"symptom: {cand.severity_note or '(unspecified)'}",
            f"diff-ref: {diff_ref}",
        ]
    )


def bug_pr_description(
    *,
    proposal: Proposal,
    bug_res: BugGateResult,
    cycle: int,
    base_anchor: str,
    fingerprint: str,
    diff_ref: str,
) -> str:
    """Author the full bug-fix CR description with the RED→GREEN correctness narrative
    (06_*.md §4.2): the defect + symptom, the reproducing test that was RED-on-base →
    GREEN-on-fix, the STAYGREEN suite result, the blast radius, and the static-triage
    results. This replaces the perf CR's "A/B delta vs anchors" narrative with a boolean
    one (the bug track has no measured delta / noise band, 05_*.md §6.1)."""
    cand = proposal.candidate
    rt = cand.reproducing_test
    test_id = rt.test_id if rt else "(none)"
    blast = ", ".join(cand.blast_radius) or "(unspecified)"
    regressions = "none" if not bug_res.failing_tests else ", ".join(bug_res.failing_tests[:10])
    return f"""# fix: {_one_line(cand.signature or proposal.description, limit=70)}

## What & why (the defect)
Defect surface: `{cand.target}`.
User-visible symptom: {cand.severity_note or '(unspecified)'}.

## Evidence it's a real fix (RED → GREEN, reproduced)
- reproducing test: `{test_id}`
- RED on base:  test FAILED on base (x2 flake check) — proves the test reproduces the defect
- GREEN on fix: test PASSES on the fix — proves the fix resolves it
- STAYGREEN:    full suite stayed green under the fix (regressions: {regressions})

## Correctness
- static triage: build_ok={bug_res.build_ok}  lint_ok={bug_res.lint_ok}  collected={bug_res.collected}
- gate reason: {bug_res.reason}

## Diff
- diff-ref: {diff_ref}   (also attached as the review revision)
- blast radius (files touched): {blast}

## Provenance
- kind: {cand.kind}   base: {base_anchor}   cycle: {cycle}   fingerprint: {fingerprint}
"""


def commit_message(
    *,
    proposal: Proposal,
    cycle: int,
    diff_ref: str,
    verify: Measurement | None = None,
    reproduce: Measurement | None = None,
    bug_res: BugGateResult | None = None,
    primary_name: str = "",
    unit: str = "",
    guardrail_tolerances: dict[str, float] | None = None,
) -> str:
    """Dispatch to the perf or bug commit-message author by the candidate's ``kind``.

    A small convenience the driver uses so the kept-commit message and the CR description
    are produced from the same call-site (one shape, two tracks)."""
    if proposal.candidate.kind == TRACK_BUG:
        assert bug_res is not None, "bug commit message needs a BugGateResult"
        return bug_commit_message(
            proposal=proposal, bug_res=bug_res, cycle=cycle, diff_ref=diff_ref
        )
    assert (
        verify is not None and reproduce is not None
    ), "perf commit message needs verify+reproduce"
    return perf_commit_message(
        proposal=proposal,
        verify=verify,
        reproduce=reproduce,
        cycle=cycle,
        primary_name=primary_name,
        unit=unit,
        diff_ref=diff_ref,
        guardrail_tolerances=guardrail_tolerances,
    )
