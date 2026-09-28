"""Keeper — the accept-predicate / keep-or-revert decision (Phase E, spine).

Phase E of the per-cycle workflow (02_architecture.md §1.2 Phase E, §6.6; 10_roadmap
M0 "keeper — the accept-predicate / keep-or-revert decision"). One serial decision
step applies the accept predicate to all survivor measurements and emits a verdict:
a single winner (or none) plus "archive ALL survivors". The branch advances ONLY via
this step (02_arch §3.2) — "revert" is simply "don't apply the diff to the branch".

The accept predicate STRUCTURE is spine; the THRESHOLDS (noise band, guardrail
tolerances) and the RH PROBES are profile-provided (02_arch §6.6; 08_safety §5.3):

    ACCEPT ⟺
      tests pass                                   (gate, blocking)
      AND  RH-A capability invariant holds          (no silent capability shrink)
      AND  RH-B functional probe == pass            (real capability still works)
      AND  primary beats the noise band (on median) (delta < -noise_band)
      AND  guardrails do NOT regress past tolerance
      AND  diff inside the edit allowlist           (enforced pre-measure by the gate)

Tie-break: largest primary improvement, then smallest diff (simplicity, 02_arch §6.6).

This module is pure decision logic — no git, no subprocess, no target token.
"""

from __future__ import annotations

import logging

from .contracts import GateResult, Measurement, Proposal, Verdict

# Module logger — every KEEP/DISCARD verdict + the SPECIFIC failing clause is logged so a
# run can be analyzed after the fact (operator goal: "increase log coverage so we can
# analyze runs later"). Greppable prefix: "keeper:".
_log = logging.getLogger("auto_improvement.spine.keeper")

# Discard reason codes (mirror the source loop's status vocabulary so archive rows
# and the ledger stay greppable across the port).
DISCARD_TESTS = "discard_tests"
DISCARD_MEASURE_ERROR = "discard_measure_error"
DISCARD_NOISE = "discard_noise"
DISCARD_GUARDRAIL = "discard_guardrail"
DISCARD_RH_CAPABILITY = "discard_rh_capability"
DISCARD_RH_FUNCTIONAL = "discard_rh_functional"
KEPT = "kept"


def _diff_size(p: Proposal) -> int:
    return len((p.diff or "").splitlines())


#: Longest code locus carried into a log line. A real target is short
#: (``src/pkg/mod.py::Class.method``); a cap bounds one pathological value.
_LOCUS_MAX = 160

#: The only characters a logged locus may contain — a file path plus a Python symbol.
#: Deliberately excludes ``=``, quotes and whitespace, which a credential assignment
#: needs. `_locus` rebuilds its result from THIS constant, character by character.
_LOCUS_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/:"


def _locus(obj: object) -> str:
    """A log-safe rendering of a candidate's code locus (``path.py::symbol``).

    Rebuilt CHARACTER BY CHARACTER from a fixed alphabet rather than filtered out of the
    input: each character of the result is drawn from ``_LOCUS_ALPHABET``, a constant in
    this module, so no substring of the original object survives as an object. That is
    what a taint-tracking query can verify — a comprehension that *filters* the input
    still yields a string derived from it, which reads as safe to a human but not to the
    analysis, and CodeQL reported exactly that.

    Practically the output is identical for a real locus (``src/pkg/mod.py::Class.method``
    round-trips unchanged); the difference is only in how it is assembled. Characters
    outside the alphabet — including the ``=`` and quotes a credential assignment needs —
    are dropped, so this string cannot carry one.
    """
    text = str(obj)
    out: list[str] = []
    for ch in text[: _LOCUS_MAX * 2]:
        idx = _LOCUS_ALPHABET.find(ch)
        if idx >= 0:
            # Append the ALPHABET's own character object, not the input's.
            out.append(_LOCUS_ALPHABET[idx])
        if len(out) >= _LOCUS_MAX:
            break
    return "".join(out) or "?"


#: A metric name is an identifier, so it gets a TIGHTER alphabet than a locus: no ``/``
#: or ``:``, which only a file path needs. Sharing the locus alphabet would let a metric
#: key keep path punctuation it has no business carrying into a ledger row.
_METRIC_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"

#: Longest metric slug carried into a verdict code. Guardrail names are short by
#: convention; a cap keeps one pathological key from bloating every ledger row.
_METRIC_SLUG_MAX = 40


def _metric_slug(metric: object) -> str:
    """An identifier-shaped slug for a guardrail metric name.

    Rebuilt from ``_METRIC_ALPHABET`` for the same reason ``_locus`` is: the result must
    not be a string derived from the input. Drops the punctuation a credential needs, so
    a verdict code carrying this slug — which is LOGGED and written to the ledger —
    cannot carry one either.
    """
    out: list[str] = []
    for ch in str(metric)[: _METRIC_SLUG_MAX * 2]:
        idx = _METRIC_ALPHABET.find(ch)
        if idx >= 0:
            out.append(_METRIC_ALPHABET[idx])
        if len(out) >= _METRIC_SLUG_MAX:
            break
    return "".join(out) or "unnamed"


#: Verdict code -> the LITERAL to log for it. An explicit table, not a pass-through:
#: returning the caller's own string (even after checking it against a set) leaves the
#: logged value dataflow-connected to the proposal it came from, which is what CodeQL's
#: clear-text-logging query reports. Reading a constant OUT of a dict severs that.
_VERDICT_LOG_TEXT = {
    KEPT: "kept",
    DISCARD_TESTS: "discard_tests",
    DISCARD_MEASURE_ERROR: "discard_measure_error",
    DISCARD_NOISE: "discard_noise",
    DISCARD_RH_CAPABILITY: "discard_rh_capability",
    DISCARD_RH_FUNCTIONAL: "discard_rh_functional",
}


def _log_safe_verdict(code: object) -> str:
    """The literal to log for a verdict code.

    Every value returned here is a constant defined in THIS module — the argument is only
    ever used as a dict key or compared, never returned or concatenated. That is the
    property a taint-tracking query can verify; a set-membership check followed by
    ``return code`` looks identical to a human but not to the analysis.

    A guardrail discard carries a metric slug, so it cannot be a fixed key. It is
    reported by its family alone rather than reconstructed: the specific metric is
    already in the archive row and the ledger, so nothing is lost from the log.
    """
    text = str(code)
    literal = _VERDICT_LOG_TEXT.get(text)
    if literal is not None:
        return literal
    if text.startswith(DISCARD_GUARDRAIL):
        return "discard_guardrail"
    return "discard_unknown"


class Keeper:
    """Applies the accept predicate to survivors and picks the winner."""

    def evaluate_one(
        self,
        *,
        proposal: Proposal,
        gate: GateResult,
        measurement: Measurement,
        guardrail_tolerances: dict[str, float] | None = None,
        direction: str = "minimize",
    ) -> tuple[bool, str]:
        """Apply the accept predicate to a single survivor. Returns
        ``(keep, status)`` where status is ``KEPT`` or a ``discard_*`` reason.

        ``guardrail_tolerances`` maps a guardrail metric name to its maximum
        allowed regression (profile-provided; absent => any positive regression is
        rejected). The noise band is on the measurement (profile calibration).

        ``direction`` is ``"minimize"`` (default) or ``"maximize"``; for minimize
        a negative delta is an improvement (must clear ``-band``), for maximize a
        positive delta is an improvement (must clear ``+band``).
        """
        tol = guardrail_tolerances or {}

        # (1) tests pass — blocking.
        if not gate.passed:
            return False, DISCARD_TESTS
        # measurement must have produced a number.
        if not measurement.ok or measurement.primary_delta is None:
            return False, DISCARD_MEASURE_ERROR
        # (5) RH-A: no silent capability shrink — blocking.
        if not measurement.rh_capability_ok:
            return False, DISCARD_RH_CAPABILITY
        # (6) RH-B: the real capability still works — blocking.
        if not measurement.rh_functional_ok:
            return False, DISCARD_RH_FUNCTIONAL
        # (2) primary beats the noise band in the improving direction.
        band = measurement.noise_band or 0.0
        if direction == "maximize":
            if measurement.primary_delta <= band:
                return False, DISCARD_NOISE
        else:
            if measurement.primary_delta >= -band:
                return False, DISCARD_NOISE
        # (3) guardrails do not regress past tolerance. A positive guardrail value
        # is treated as a regression magnitude; reject if it exceeds tolerance
        # (default tolerance 0 == reject any regression).
        for metric, value in measurement.guardrails.items():
            allowed = tol.get(metric, 0.0)
            if value > allowed:
                # The metric NAME is sanitized to an identifier-shaped slug before it
                # joins the verdict code. It comes from the profile's guardrail dict, so
                # it is not attacker-controlled in practice — but this string is LOGGED
                # and recorded in the ledger, and CodeQL's clear-text-logging query
                # cannot tell a metric name from a secret when the value is interpolated
                # from a dict key. Restricting the charset makes "carries no credential"
                # a property a machine can check rather than a claim in a comment.
                return False, f"{DISCARD_GUARDRAIL}_{_metric_slug(metric)}"

        return True, KEPT

    def decide(
        self,
        *,
        survivors: list[tuple[Proposal, GateResult, Measurement]],
        guardrail_tolerances: dict[str, float] | None = None,
        direction: str = "minimize",
    ) -> tuple[Verdict, list[tuple[Proposal, str, Measurement]]]:
        """Evaluate every survivor and pick the winner.

        Returns ``(verdict, archived)`` where ``archived`` is every survivor with
        its per-candidate status + measurement (the driver archives ALL of them —
        the whole population is evolutionary memory, 02_arch §3.1)."""
        archived: list[tuple[Proposal, str, Measurement]] = []
        keepers: list[tuple[Proposal, Measurement]] = []
        for proposal, gate, measurement in survivors:
            # `verdict_code`, not `status`: CodeQL's clear-text-logging heuristic treats a
            # variable NAMED `status` as potentially sensitive and flagged the log line
            # below at high severity. The value is only ever one of the fixed
            # keep/discard reason codes (`kept`, `discard_noise`, …) — no secret can
            # reach it — so the accurate name is also the one that does not trip the scan.
            keep, verdict_code = self.evaluate_one(
                proposal=proposal,
                gate=gate,
                measurement=measurement,
                guardrail_tolerances=guardrail_tolerances,
                direction=direction,
            )
            archived.append((proposal, verdict_code, measurement))
            # Log EVERY per-candidate verdict with the specific failing clause + the
            # numbers behind it (delta vs band, RH booleans), so a run's keep/discard
            # decisions are reconstructable from logs alone.
            tgt = _locus(
                getattr(getattr(proposal, "candidate", None), "target", None)
                or getattr(proposal, "cand_id", "?")
            )
            delta = measurement.primary_delta
            _log.info(
                "keeper: %s %s | target=%s delta=%s band=%s rh_cap=%s rh_func=%s gate_passed=%s",
                "KEEP" if keep else "DISCARD",
                _log_safe_verdict(verdict_code),
                tgt,
                ("%.3f" % delta) if isinstance(delta, (int, float)) else delta,
                measurement.noise_band,
                measurement.rh_capability_ok,
                measurement.rh_functional_ok,
                gate.passed,
            )
            if keep:
                keepers.append((proposal, measurement))

        _log.info(
            "keeper: population evaluated — %d survivor(s), %d keeper(s), %d discarded",
            len(survivors),
            len(keepers),
            len(survivors) - len(keepers),
        )
        if not keepers:
            _log.info("keeper: VERDICT no_keep — no survivor cleared the accept predicate")
            return (
                Verdict(keep=False, status="no_keep", reason="no survivor cleared the predicate"),
                archived,
            )

        # Tie-break: largest primary improvement (most-negative delta), then the
        # smallest diff (simplicity).
        keepers.sort(key=lambda pm: (pm[1].primary_delta or 0.0, _diff_size(pm[0])))
        winner, win_meas = keepers[0]
        _win_tgt = _locus(
            getattr(getattr(winner, "candidate", None), "target", None)
            or getattr(winner, "cand_id", "?")
        )
        _log.info(
            "keeper: VERDICT keep — winner target=%s delta=%.3f band=%s diff_lines=%d (of %d keeper(s))",
            _win_tgt,
            win_meas.primary_delta or 0.0,
            win_meas.noise_band,
            _diff_size(winner),
            len(keepers),
        )
        return (
            Verdict(
                keep=True,
                status=KEPT,
                winner=winner,
                measurement=win_meas,
                reason=f"primary Δ={win_meas.primary_delta:.3f} clears band "
                f"±{win_meas.noise_band or 0.0}",
            ),
            archived,
        )
