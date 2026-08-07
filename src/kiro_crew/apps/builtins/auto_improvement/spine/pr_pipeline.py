"""CR-emission pipeline — verify → REPRODUCE → DRAFT-CR → ledger (spine, milestone M5).

The *output stage* of Phase 2 (06_cr_generation_and_dedup.md §0–§1): once a track's keep
decision has accepted a candidate (perf: keep-or-revert A/B beat the band + held guardrails;
bug: RED/GREEN gate), THIS module turns it into a reviewable **draft CR** and records the
outcome in the dedup ledger. It is the realized §1.3 ``emit_cr`` flow, lifted out of the
driver so the *boundary* is one testable spine surface.

The three confirmations a change must survive before a CR exists (06_*.md §1.1):
  GATE      candidate builds/imports cleanly                 (already run by the driver's
            Phase-C gate before a candidate reaches here)
  VERIFY    perf: A/B beats the band; bug: RED→GREEN          (the track keep decision)
  REPRODUCE perf: a SECOND independent A/B, same direction,   (06_*.md §1.1 "kills first-run
            still beats the band; bug: the doubled-RED         flukes"; the bug track's
            flake check inside the gate IS the reproduce       doubled-RED is its analogue)
  DRAFT CR  author the description + pr_recipe.draft()         (draft / --no-open on share/)
  LEDGER    record filed | failed_verify | duplicate | error

THE DEDUP INVARIANT (06_*.md §2.1, exactly): *the runner never re-files a known finding,
across restarts.* Every finding is reduced to a stable content fingerprint; before a CR is
filed the spine checks the fingerprint against the append-only ledger; a fingerprint already
present is **not re-processed and never produces a second CR**. The primary dedup pre-filter
runs in the driver BEFORE expensive work (§2.4 "dedup happens before expensive work"); the
``emit_*`` methods here run a SECOND, defense-in-depth short-circuit at the CR boundary
(§1.3 ``status="duplicate"``), so even a same-cycle double-keep on one locus files once.

THE INVARIANT (06_*.md §1.2): orchestration + gating + verification are deterministic Python,
NOT model judgment. The model only writes/expands the CR prose (06_*.md §3.2); it never
decides whether a CR is warranted. This module is pure spine control flow + deterministic
description authoring (:mod:`.pr_description`); the *draft mechanism* is the profile's
``pr_recipe`` (§5: spine owns "when"; profile owns "how to file a draft in this review
system"). The CR is DRAFT-only / never published-or-merged — that policy is enforced here at
the spine boundary, realized by the recipe (§4, §5.3; 08_safety §6).

Target-agnostic (10_roadmap M5 generalization note; M0/M1 grep): this module names no build
tool, no provider, no config flag, no Kiro Crew path. The CR-recipe (how/where to draft) and
the ruler field names are profile-shaped; the verify→reproduce→draft→ledger control flow and
the fingerprint contract are spine.

Docs: 06_cr_generation_and_dedup.md §1 (pipeline), §2 (dedup ledger), §3 (CR authoring),
§4 (draft/--no-open on share/), §5 (CR recipe as a profile hook); 10_roadmap M5.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from pathlib import Path

from . import ledger as L
from .contracts import BugGateResult, Measurement, Proposal, TargetProfile
from .pr_description import bug_cr_title, bug_pr_description, perf_cr_title, perf_pr_description


@dataclass
class CrOutcome:
    """The result of an ``emit_*`` call — the durable record the driver logs/counts.

    ``status`` is a ledger status (``filed`` | ``failed_verify`` | ``duplicate`` |
    ``error``). ``cr`` is the CR id (or ``QUEUED:<fp>`` if drafting was unavailable but the
    queue copy is on disk — 06_*.md §5.3 idempotency). ``filed`` is True iff a draft CR was
    created this call.

    ``reproduce`` carries the SECOND, independent A/B :class:`Measurement` the pipeline ran
    (perf track) so the driver's kept-commit message records the REAL reproduce numbers —
    not a re-use of VERIFY. Before this, the commit message echoed the VERIFY delta on its
    ``reproduce:`` line because the outcome carried no reproduce measurement; now the
    independently-computed reproduce delta the pipeline already produced is carried back
    (06_*.md §3.1/§3.2 "commit and CR never disagree" — both from the same harness output).
    It is ``None`` for the bug track (no second A/B — the doubled-RED flake check is the
    reproduce analogue, §1.1) and on a non-``filed`` outcome."""

    fp: str
    status: str
    cr: str = ""
    note: str = ""
    filed: bool = False
    reproduce: Measurement | None = None
    # F10 direct-commit mode: the change passed EVERY gate (dedup + reproduce/RED-GREEN) and
    # its description is authored, but instead of drafting a CR the pipeline left it for the
    # driver to commit + push to the authorized branch. When True, the pipeline did NOT draft
    # and did NOT record a terminal ledger row — the DRIVER records ``committed`` with the
    # resulting sha after the push (so the ledger carries the real sha, not a placeholder).
    # ``filed`` stays False (no CR); ``reproduce`` is still carried for the commit message.
    committed_ready: bool = False


class CrPipeline:
    """The verify → REPRODUCE → draft-CR → ledger boundary (the §1.3 ``emit_cr``).

    Constructed once per run with the ledger + the profile's CR recipe + the measurer (for
    the perf REPRODUCE A/B). The driver calls :meth:`emit_perf` on a kept perf winner and
    :meth:`emit_bug` on an accepted bug fix; each returns a :class:`CrOutcome` and has
    already appended exactly one terminal ledger row (filed / failed_verify / duplicate /
    error). The pipeline NEVER decides whether a candidate is good — that is the keeper /
    bug gate; it confirms the keep reproduces, authors the description, and files the draft.
    """

    def __init__(
        self,
        *,
        ledger: L.Ledger,
        measurer,
        guardrail_tolerances: dict[str, float] | None = None,
        logger: logging.Logger | None = None,
        direct_commit: bool = False,
        ruler_proven: bool = True,
    ) -> None:
        self.ledger = ledger
        self.measurer = measurer  # spine Measurer — supplies the REPRODUCE A/B
        self.guardrail_tolerances = guardrail_tolerances or {}
        #: Whether the Phase-1 canary cleared the band. False => the perf PR body carries a
        #: "ruler not proven on this target" caveat, because the band is then a lower bound
        #: on sensitivity rather than proof. Defaults True so a caller that does not know
        #: (a stub, a unit test) keeps today's wording rather than crying wolf.
        self.ruler_proven = bool(ruler_proven)
        self.log = logger or logging.getLogger("auto_improvement.pr_pipeline")
        # F10: when on, a verified+reproduced winner is NOT drafted as a CR — the pipeline
        # returns a ``committed_ready`` outcome and the driver commits + pushes it to the
        # authorized branch, then records the ``committed`` ledger row with the real sha. The
        # verify/reproduce/RED-GREEN gates still run unchanged (they precede the draft step) —
        # direct-commit changes WHAT happens to a verified change, never WHETHER it's verified.
        self.direct_commit = bool(direct_commit)

    # ── dedup short-circuit (§1.3 top of emit_cr; §2.1 invariant) ────────────

    def _duplicate_guard(self, fp: str, *, kind: str, target: str) -> CrOutcome | None:
        """Defense-in-depth dedup at the CR boundary (06_*.md §1.3 / §2.4).

        Returns a ``duplicate`` outcome (and records it) iff this fingerprint already has a
        TERMINAL outcome (filed / a prior verify/gate failure / error / another duplicate).
        A fingerprint that is only ``seen`` (the in-flight marker the driver writes before
        working a candidate) is NOT a duplicate — it is THIS candidate mid-flight, so we let
        it proceed to be filed. This is what makes the same finding re-discovered after a
        RESTART collide (its terminal row reloads from JSONL) while not blocking the live
        candidate that is being filed right now."""
        st = self.ledger.status_of(fp)
        if st is not None and st != L.STATUS_SEEN:
            self.log.info("dedup at CR boundary: fp=%s already %s — not re-filing", fp, st)
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_DUPLICATE,
                    note=f"already {st}; CR not re-filed (dedup invariant §2.1)",
                )
            )
            return CrOutcome(fp=fp, status=L.STATUS_DUPLICATE, note=f"already {st}")
        return None

    # ── perf track: verify (already done) → REPRODUCE → draft ────────────────

    def emit_perf(
        self,
        *,
        profile: TargetProfile,
        winner: Proposal,
        verify: Measurement,
        cycle: int,
        gated_commit_sha: str,
        diff_ref: str,
        base_anchor: str,
        parent_ref: str = "",
    ) -> CrOutcome:
        """Run REPRODUCE then draft a perf CR for a kept winner (06_*.md §1.3, perf).

        ``verify`` is the VERIFY measurement the keeper accepted. We run a SECOND
        independent A/B (REPRODUCE) via the measurer; if it does NOT beat the band in the
        SAME direction, the win was a first-run fluke → record ``failed_verify``, no CR
        (06_*.md §1.1). Otherwise author the full attributable description (§3.2) and file a
        draft CR via the profile's ``pr_recipe`` (§4/§5). Records exactly one terminal
        ledger row and returns its :class:`CrOutcome`."""
        cand = winner.candidate
        fp = L.fingerprint(kind=cand.kind, target=cand.target)

        dup = self._duplicate_guard(fp, kind=cand.kind, target=cand.target)
        if dup is not None:
            return dup

        # REPRODUCE — the second, independent A/B (the anti-fluke step).
        try:
            reproduce = self.measurer.reproduce(
                profile=profile,
                proposal=winner,
                gated_commit_sha=gated_commit_sha,
            )
        except Exception as e:  # noqa: BLE001
            self.log.error("REPRODUCE errored for %s: %s", cand.target, e)
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=cand.kind,
                    target=cand.target,
                    status=L.STATUS_ERROR,
                    note=f"reproduce error: {e}"[:200],
                )
            )
            return CrOutcome(fp=fp, status=L.STATUS_ERROR, note="reproduce error")

        if not self._reproduces(verify, reproduce):
            self.log.warning(
                "did NOT reproduce (fluke): verify Δ=%s reproduce Δ=%s — no CR",
                verify.primary_delta,
                reproduce.primary_delta,
            )
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=cand.kind,
                    target=cand.target,
                    status=L.STATUS_FAILED_VERIFY,
                    note=f"first-run fluke: v1Δ={verify.primary_delta} v2Δ={reproduce.primary_delta}"[
                        :200
                    ],
                )
            )
            return CrOutcome(fp=fp, status=L.STATUS_FAILED_VERIFY, note="not reproduced")

        # Verified + reproduced → author the full attributable CR description (§3.2).
        description = perf_pr_description(
            proposal=winner,
            verify=verify,
            reproduce=reproduce,
            cycle=cycle,
            base_anchor=base_anchor,
            fingerprint=fp,
            primary_name=profile.ruler.primary_name,
            unit=profile.ruler.unit,
            diff_ref=diff_ref,
            guardrail_tolerances=self.guardrail_tolerances,
            ruler_proven=self.ruler_proven,
        )
        summary = perf_cr_title(
            winner, verify, primary_name=profile.ruler.primary_name, unit=profile.ruler.unit
        )
        return self._draft_and_record(
            profile=profile,
            fp=fp,
            kind=cand.kind,
            target=cand.target,
            summary=summary,
            description=description,
            diff=winner.diff,
            note=f"verified+reproduced Δ={verify.primary_delta}",
            parent_ref=parent_ref,
            # Carry the INDEPENDENTLY-computed REPRODUCE measurement back so the driver's
            # kept-commit message uses the real reproduce numbers, not a re-use of VERIFY
            # (06_*.md §3.1/§3.2; CrOutcome.reproduce). Bug track passes None (no 2nd A/B).
            reproduce=reproduce,
        )

    @staticmethod
    def _reproduces(verify: Measurement, reproduce: Measurement) -> bool:
        """True iff REPRODUCE confirms VERIFY: ran cleanly, SAME DIRECTION, and STILL beats
        the calibrated band (06_*.md §1.1). Same band as VERIFY (the ruler's calibration);
        a delta that flips sign or drops inside the band is a fluke."""
        if not reproduce.ok or reproduce.primary_delta is None or verify.primary_delta is None:
            return False
        # Use the reproduce measurement's OWN calibrated band; fall back to verify's only when
        # reproduce has none. A band of exactly 0.0 is a LEGITIMATE calibrated value (single-
        # sample fast calibration with a 0.0 floor → compute_noise_band returns the floor), so
        # we must test ``is None``, NOT truthiness — ``x or y`` would collapse a real 0.0 and
        # wrongly substitute verify's (larger) band, rejecting a genuinely reproduced win.
        if reproduce.noise_band is not None:
            band = reproduce.noise_band
        elif verify.noise_band is not None:
            band = verify.noise_band
        else:
            band = 0.0
        same_direction = (reproduce.primary_delta < 0) == (verify.primary_delta < 0)
        beats_band = abs(reproduce.primary_delta) > band  # for a minimize metric: |Δ| > band
        return same_direction and beats_band

    # ── bug track: RED/GREEN (already done) → draft (no second A/B) ──────────

    def emit_bug(
        self,
        *,
        profile: TargetProfile,
        winner: Proposal,
        bug_res: BugGateResult,
        cycle: int,
        diff_ref: str,
        base_anchor: str,
        parent_ref: str = "",
    ) -> CrOutcome:
        """Draft a bug-fix CR for a candidate that passed RED ∧ GREEN ∧ STAYGREEN.

        The bug track has NO second A/B (06_*.md §1.1): the doubled-RED flake check INSIDE
        the RED/GREEN gate is the reproduction analogue, so by the time a bug winner reaches
        here it is already reproduced. We dedup-guard, author the RED→GREEN correctness
        narrative (§4.2), and file the draft. Records exactly one terminal ledger row."""
        cand = winner.candidate
        fp = L.fingerprint(kind=cand.kind, target=cand.target)

        dup = self._duplicate_guard(fp, kind=cand.kind, target=cand.target)
        if dup is not None:
            return dup

        description = bug_pr_description(
            proposal=winner,
            bug_res=bug_res,
            cycle=cycle,
            base_anchor=base_anchor,
            fingerprint=fp,
            diff_ref=diff_ref,
        )
        summary = bug_cr_title(cand)
        return self._draft_and_record(
            profile=profile,
            fp=fp,
            kind=cand.kind,
            target=cand.target,
            summary=summary,
            description=description,
            diff=winner.diff,
            note=f"bug RED→GREEN→STAYGREEN ({bug_res.reason})",
            parent_ref=parent_ref,
        )

    # ── the shared draft + record step (§4 draft/--no-open; §5.3 idempotency) ─

    @staticmethod
    def _draft_accepts_parent_ref(draft) -> bool:
        """True iff the recipe's ``draft`` accepts a ``parent_ref`` keyword.

        Probes the signature (back-compat: older recipes predate the param, §profile.PRRecipe).
        A ``draft(**kwargs)`` recipe is treated as accepting it (the kwargs absorb it). If the
        signature can't be introspected (e.g. a builtin/C callable), we conservatively assume
        it does NOT, so the call stays on the older arity and a body error is never masked."""
        try:
            sig = inspect.signature(draft)
        except (TypeError, ValueError):
            return False
        params = sig.parameters
        if "parent_ref" in params:
            return True
        return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    def _write_queue_copy(
        self, *, profile: TargetProfile, fp: str, summary: str, description: str, diff: str
    ) -> None:
        """Write the durable ``pr_queue/<fp>.pr.md`` + ``<fp>.diff`` for a DIRECT-COMMITTED
        fix, mirroring the format the recipe writes for a filed CR — so the UI's CR-detail
        panel renders a direct-committed fix's description + diff identically to a CR-based
        one (operator: committed items must show a description where CR changes show theirs).
        Best-effort: a write failure must never block the commit/push that follows. Reuses
        the profile's recipe pr_queue_dir so filed + committed copies live in one place."""
        try:
            recipe = getattr(profile, "pr_recipe", None)
            qdir = getattr(recipe, "pr_queue_dir", None)
            if qdir is None:
                return

            qdir = Path(qdir)
            qdir.mkdir(parents=True, exist_ok=True)
            (qdir / f"{fp}.diff").write_text(diff or "")
            # Same shape as the recipe's queue copy ("# <summary>\n\n<description>"); strip
            # a leading H1 in the description so we don't render two titles.
            body = description or ""
            if body.lstrip().startswith("# "):
                body = body.split("\n", 1)[1] if "\n" in body else ""
            # `.pr.md`, NOT `.cr.md`: the display reader (routes._handle_finding_detail,
            # commit.py) opens `<fp>.pr.md`, and the `.cr.md → .pr.md` rename (see store.py)
            # never reached this writer — so a direct-committed fix's description was written
            # to a filename nothing reads and silently never rendered. Raised by the GPT
            # review of this branch.
            (qdir / f"{fp}.pr.md").write_text(f"# {summary}\n\n{body}\n")
        except Exception:  # noqa: BLE001 — the queue copy is for display; never fatal
            self.log.debug("direct-commit queue-copy write failed for %s", fp, exc_info=True)

    def _draft_and_record(
        self,
        *,
        profile: TargetProfile,
        fp: str,
        kind: str,
        target: str,
        summary: str,
        description: str,
        diff: str,
        note: str,
        parent_ref: str = "",
        reproduce: Measurement | None = None,
    ) -> CrOutcome:
        """Call the profile's ``pr_recipe.draft`` (draft-only) and record the outcome.

        The recipe writes the durable ``pr_queue/<fp>.diff`` + ``<fp>.pr.md`` queue copy and
        creates a DRAFT / unpublished review, returning a CR id (or ``QUEUED:<fp>`` if the
        review tool is unavailable — the queue is the record either way, §5.3 idempotency).
        On a recipe EXCEPTION the spine records ``error`` and leaves the queued artifacts on
        disk for manual recovery (§5.1 ``error``; §5.3 invariant 4). MUST NOT publish/merge —
        enforced by the recipe; the spine only ever asks for a draft (§4.1)."""
        # F10 direct-commit: skip drafting a CR entirely. The change is fully verified (every
        # gate above this point already ran); hand it back to the driver to commit + push to
        # the authorized branch. We do NOT record a ledger row here — the driver records
        # ``committed`` with the resulting sha after a SUCCESSFUL push (so a push failure
        # doesn't leave a phantom ``committed`` row, and the sha is real). The queue copy that
        # the recipe would have written is skipped too — BUT we still write the queue copy
        # (the <fp>.pr.md description + <fp>.diff) so the UI's CR-detail panel can show a
        # direct-committed fix's DESCRIPTION + diff, the same way it shows a filed CR's
        # (operator: "even for directly pushed items we need to provide description … where
        # we put CR description for CR-based changes"). A pushed commit is the durable record
        # of the CHANGE; this queue copy is the durable record of the human-readable WHY.
        if self.direct_commit:
            self.log.info("direct-commit: skipping CR draft for fp=%s — driver will push", fp)
            self._write_queue_copy(
                profile=profile, fp=fp, summary=summary, description=description, diff=diff
            )
            return CrOutcome(
                fp=fp,
                status=L.STATUS_SEEN,
                note=note,
                filed=False,
                reproduce=reproduce,
                committed_ready=True,
            )
        try:
            # Back-compat without an over-broad ``except TypeError``: decide BEFORE the call
            # whether the recipe's draft() accepts ``parent_ref`` (older profiles / test fakes
            # predate it). A signature probe distinguishes the arity case from a TypeError
            # raised INSIDE draft()'s body — the old ``except TypeError`` conflated the two and
            # silently re-ran draft() (double queue write + double draft-review, parent_ref
            # dropped), breaking the one-finding→one-draft invariant. A body TypeError now
            # propagates to the outer handler and is recorded ``error`` (draft() runs once).
            # Pass parent_ref as an explicit conditional kwarg rather than via a loose
            # dict: a dict[str, object]/[str, str|None] cannot be **-unpacked into draft()'s
            # typed (str) params without a mypy arg-type error, and adding it to a str-only
            # dict is itself a type error. Two explicit call sites keep both type-clean.
            if self._draft_accepts_parent_ref(profile.pr_recipe.draft):
                cr = profile.pr_recipe.draft(
                    summary=summary,
                    description=description,
                    diff=diff,
                    fingerprint=fp,
                    parent_ref=parent_ref or None,
                )
            else:
                cr = profile.pr_recipe.draft(
                    summary=summary,
                    description=description,
                    diff=diff,
                    fingerprint=fp,
                )
        except Exception as e:  # noqa: BLE001
            self.log.error("draft CR failed for %s: %s", target, e)
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    note=f"cr draft error: {e}"[:200],
                )
            )
            return CrOutcome(fp=fp, status=L.STATUS_ERROR, note=f"cr draft error: {e}"[:120])

        # A `QUEUED:<fp>` reference is NOT a filed pull request. `pr_recipe.draft` returns it
        # when the change is on disk but no PR could be opened (no `gh` on PATH, no network,
        # a refused push). Recording that as `filed` was doubly wrong: `filed` is HARD-terminal
        # in `Ledger.known` ("a filed CR is never re-filed"), so the locus was deduped forever
        # and never retried; and `filed_crs()` feeds the PR watchers, which were handed a
        # non-URL. Measured: `known()` returned True and `filed_crs()` returned
        # `['QUEUED:abc']`. `STATUS_ERROR` is SOFT-terminal — retryable once the cooldown
        # elapses — which is exactly "we could not file it this time". `ledger_admin` already
        # treats this prefix as not-a-real-reference; that judgement now applies here too.
        # Raised by the GPT review of this branch.
        if str(cr or "").startswith("QUEUED:"):
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp,
                    kind=kind,
                    target=target,
                    status=L.STATUS_ERROR,
                    cr=cr,
                    note=f"queued, not filed: {note}"[:200],
                )
            )
            self.log.warning("DRAFT CR QUEUED (no pull request opened): fp=%s (%s)", fp, note)
            # `filed=True` even though no PR exists, and that is deliberate. In the driver
            # `filed` means "this was a REALIZED win" — a False here also rolls the provisional
            # commit back (`_reset_provisional`) and decrements `kept`, throwing away a change
            # that passed RED×2 → GREEN → STAYGREEN just because `gh` was missing. Measured:
            # flipping it to False took a bounded run's `kept` from 1 to 0. The win is real and
            # the durable queue copy holds it; only the PUBLICATION failed, which is what the
            # retryable ledger status above now records.
            return CrOutcome(
                fp=fp,
                status=L.STATUS_ERROR,
                cr=cr,
                note=f"queued, not filed: {note}",
                filed=True,
                reproduce=reproduce,
            )

        return self._record_filed(
            fp=fp, kind=kind, target=target, cr=cr, note=note, reproduce=reproduce
        )

    def _record_filed(
        self,
        *,
        fp: str,
        kind: str,
        target: str,
        cr: str,
        note: str,
        reproduce: Measurement | None = None,
    ) -> CrOutcome:
        """Record a FILED pull request, keeping the outcome even if the ledger write fails.

        The pull request is IRREVERSIBLE by the time this runs — it exists on GitHub. Letting
        an ``OSError`` (a full disk) propagate turned a successful publish into a run error and
        recorded the PR nowhere, so the next cycle re-discovered the same locus and filed a
        DUPLICATE: the ledger is the only dedup store, and a raise leaves it with no row at all.
        Losing the row is bad; losing the row AND the outcome is worse. Logged loudly at ERROR
        because a missing row is exactly what causes that duplicate. Raised by the GPT review.
        """
        try:
            self.ledger.record(
                L.LedgerEntry(
                    fp=fp, kind=kind, target=target, status=L.STATUS_FILED, cr=cr, note=note[:200]
                )
            )
        except Exception:  # noqa: BLE001 — the PR already exists; never unpublish it by raising
            self.log.exception(
                "DRAFT CR filed but the ledger row could NOT be written: fp=%s cr=%s — this "
                "locus may be re-discovered and filed again",
                fp,
                cr,
            )
        else:
            self.log.info("DRAFT CR filed: fp=%s cr=%s (%s)", fp, cr, note)
        return CrOutcome(
            fp=fp, status=L.STATUS_FILED, cr=cr, note=note, filed=True, reproduce=reproduce
        )
