"""Backend-route tests: a worker's result record is validated before it is adopted.

Adoption is all-or-nothing. A record that names another change, breaks the schema, or holds
a value of the wrong shape or type (a non-numeric count or line, a prose finding field or
phase-one value that is not a string, a malformed nested object) is refused, and the existing
record stays intact.
"""

import json
import typing
import unittest

import pytest
from sage_lib import results, store


class TestAdoptionValidatesBeforeItWrites:
    """Adoption must be all-or-nothing.

    The ``O_TRUNC`` write that closes the symlink hole is not atomic: a malformed
    payload truncates whatever valid record is already filed, and ``read_result``
    then raises on the wreckage, so no retry can recover it. Validate first, write
    via rename.
    """

    def _stage(self, tmp_path, change_id, body):
        shared = results.results_dir(tmp_path, None)
        shared.mkdir(parents=True, exist_ok=True)
        (shared / f"{results.safe_change_id(change_id)}.json").write_text(body, encoding="utf-8")

    def _good(self, cid="CR-1", yellow=1):
        """A record that satisfies the real contract (REQUIRED_TOP/PHASE1).

        The earlier minimal fixture passed the envelope checks but was never a
        valid record, so it could not exercise adoption once the schema gate went
        in — the contract is what write_result has always enforced.
        """
        import json as _json

        return _json.dumps(
            {
                "schema": "code-review-sage-result",
                "version": 1,
                "change_id": cid,
                "platform": "github",
                "repo_identity": "github.com/o/r",
                "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
                "counts": {"red": 0, "yellow": yellow},
            }
        )

    def test_malformed_output_leaves_the_existing_record_intact(self, tmp_path):
        store.ensure_layout(tmp_path)

        # A valid record is adopted first.
        self._stage(tmp_path, "CR-1", self._good(yellow=3))
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is True
        before = results.read_result("CR-1", tmp_path, "run-1")
        assert before and before["counts"]["yellow"] == 3

        # Then the poster leaves malformed JSON for the same change.
        self._stage(tmp_path, "CR-1", "{not json at all")
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is False

        # The valid record survived and is still readable — the retry path works.
        after = results.read_result("CR-1", tmp_path, "run-1")
        assert after == before, "a malformed payload destroyed the valid record"

    def test_a_record_violating_the_schema_is_refused(self, tmp_path):
        store.ensure_layout(tmp_path)
        # `phase1: []` is dict-shaped at the top level but carries a list where an
        # object belongs. The report reads it as rec.get("phase1", {}).get(...),
        # which raises on a list — the present-but-wrong-type case a default
        # cannot rescue, and the whole run fails.
        bad = json.dumps(
            {
                "schema": "code-review-sage-result",
                "version": 1,
                "change_id": "CR-1",
                "platform": "github",
                "repo_identity": "github.com/o/r",
                "phase1": [],
            }
        )
        self._stage(tmp_path, "CR-1", bad)
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is False
        assert results.read_result("CR-1", tmp_path, "run-1") is None

    def test_an_unknown_gate_verdict_is_refused(self, tmp_path):
        store.ensure_layout(tmp_path)
        bad = json.dumps(
            {
                "schema": "code-review-sage-result",
                "version": 1,
                "change_id": "CR-1",
                "platform": "github",
                "repo_identity": "github.com/o/r",
                "phase1": {
                    "gate_verdict": "LOOKS_FINE",
                    "design_risk": "low",
                    "criticality": "low",
                },
            }
        )
        self._stage(tmp_path, "CR-1", bad)
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is False

    def test_a_record_naming_another_change_is_refused(self, tmp_path):
        store.ensure_layout(tmp_path)
        # Filed under CR-1 but claiming to be CR-9: accepting it would attribute
        # CR-9's findings to CR-1 in the report.
        self._stage(tmp_path, "CR-1", self._good("CR-9"))
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is False
        assert results.read_result("CR-1", tmp_path, "run-1") is None

    def test_a_json_array_is_refused(self, tmp_path):
        store.ensure_layout(tmp_path)
        self._stage(tmp_path, "CR-1", '[{"change_id": "CR-1"}]')
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is False

    def test_no_temp_files_are_left_behind(self, tmp_path):
        store.ensure_layout(tmp_path)
        self._stage(tmp_path, "CR-1", self._good())
        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is True
        rd = results.results_dir(tmp_path, "run-1")
        assert not list(rd.glob(".adopt-*")), "adoption left a temp file behind"


class TestAdoptionRejectsMalformedNestedShapes:
    """Validation must reject nested values the render path dereferences.

    `validate_result` checked that `phase1` was an object and `findings` a list,
    but not that `counts` / `blast_radius` were objects or that each finding entry
    was one. A worker writing `"counts": [1]` therefore passed validation, was
    adopted, and then aborted the run with an AttributeError when `counts.get()`
    ran at render time -- the review was already paid for and produced no report.
    """

    def _record(self, **over):
        rec = {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": "CR-1",
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": {"red": 0, "yellow": 0},
            "blast_radius": {"rating": "SMALL", "signals": {}},
            "findings": [],
        }
        rec.update(over)
        return rec

    def test_a_valid_record_still_passes(self):
        assert results.validate_result(self._record()) == []

    @pytest.mark.parametrize("bad", [[1], "red=0", 3, True])
    def test_non_object_counts_is_rejected(self, bad):
        errs = results.validate_result(self._record(counts=bad))
        assert any("counts must be an object" in e for e in errs), errs

    @pytest.mark.parametrize("bad", [["SMALL"], "SMALL", 0])
    def test_non_object_blast_radius_is_rejected(self, bad):
        errs = results.validate_result(self._record(blast_radius=bad))
        assert any("blast_radius must be an object" in e for e in errs), errs

    def test_a_non_object_finding_entry_is_rejected(self):
        errs = results.validate_result(
            self._record(findings=[{"severity": "red"}, "not-an-object"])
        )
        assert any("findings[1] must be an object" in e for e in errs), errs

    def test_absent_optional_shapes_are_still_valid(self):
        """counts / blast_radius are optional -- absent is not malformed."""
        rec = self._record()
        del rec["counts"]
        del rec["blast_radius"]
        assert results.validate_result(rec) == []

    def test_write_result_refuses_a_malformed_record(self, tmp_path):
        """The same validator gates the write path, so the bad shape never lands."""

        store.ensure_layout(tmp_path)
        with pytest.raises(ValueError, match="counts must be an object"):
            results.write_result(self._record(counts=[1]), tmp_path, "run-v1")


class TestAdoptionRequiresAnExactChangeIdentity:
    """Adoption must compare change ids EXACTLY, not through `safe_change_id`.

    That sanitizer is lossy by design — it produces a filename stem — so
    comparing the sanitized forms accepted a record naming a genuinely different
    change. `GH-acme-service/api-1` and `GH-acme-service_api-1` both reduce to
    `GH-acme-service_api-1`, so a worker record for the first was filed as the
    second and the report attributed its findings to the wrong pull request.
    """

    def _record(self, cid):
        return {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": cid,
            "platform": "github",
            "repo_identity": "github.com/acme/service_api",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": {"red": 0, "yellow": 0},
            "findings": [],
        }

    def test_a_record_naming_a_different_change_is_refused(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        want = "GH-acme-service_api-1"
        other = "GH-acme-service/api-1"  # different change, same stem
        assert results.safe_change_id(other) == results.safe_change_id(want)
        assert other != want

        shared = results.results_dir(tmp_path, None)
        shared.mkdir(parents=True, exist_ok=True)
        (shared / f"{results.safe_change_id(want)}.json").write_text(
            json.dumps(self._record(other)), encoding="utf-8"
        )

        assert results.adopt_from_shared(want, tmp_path, "run-x1") is False

    def test_an_exact_match_is_still_adopted(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        want = "GH-acme-service_api-1"
        shared = results.results_dir(tmp_path, None)
        shared.mkdir(parents=True, exist_ok=True)
        (shared / f"{results.safe_change_id(want)}.json").write_text(
            json.dumps(self._record(want)), encoding="utf-8"
        )

        assert results.adopt_from_shared(want, tmp_path, "run-x2") is True


class TestCountValuesMustBeNumeric:
    """`counts` values are used in arithmetic, so a string count aborts the run.

    `report.py` scores with `counts.get("red", 0) * 15 + counts.get("yellow", 0)
    * 5`. A worker writing `{"red": "1"}` satisfied the object check added for
    the non-object case, was adopted, and then raised TypeError at scoring time —
    the same failure one level deeper, after the review was already paid for.
    """

    def _record(self, counts):
        return {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": "CR-1",
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": counts,
            "findings": [],
        }

    @pytest.mark.parametrize("bad", ["1", None, [1], {"n": 1}, True, False])
    def test_a_non_numeric_count_is_rejected(self, bad):
        from sage_lib import results

        errs = results.validate_result(self._record({"red": bad, "yellow": 0}))
        assert any("counts.red must be a number" in e for e in errs), errs

    @pytest.mark.parametrize("good", [0, 3, 3.0])
    def test_numeric_counts_pass(self, good):
        from sage_lib import results

        assert results.validate_result(self._record({"red": good, "yellow": 0})) == []

    def test_every_band_is_checked_not_just_red_and_yellow(self):
        """A new band must not reintroduce the gap."""
        from sage_lib import results

        errs = results.validate_result(self._record({"red": 0, "yellow": 0, "green": "many"}))
        assert any("counts.green must be a number" in e for e in errs), errs

    def test_the_scoring_arithmetic_really_does_raise(self):
        """The premise: this is why the value type matters."""
        # Typed loosely on purpose — the point is the RUNTIME failure a worker's
        # string count causes in report scoring, which a static check would
        # otherwise refuse to let us demonstrate.
        counts: dict[str, typing.Any] = {"red": "1", "yellow": 0}
        with pytest.raises(TypeError):
            counts.get("red", 0) * 15 + counts.get("yellow", 0) * 5

    def test_write_result_refuses_a_string_count(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        with pytest.raises(ValueError, match="counts.red must be a number"):
            results.write_result(self._record({"red": "1", "yellow": 0}), tmp_path, "run-c1")


class TestNonScalarFindingFieldsAreRefused:
    """The boundary rejects what the redactor would otherwise have to sanitize.

    `validate_result` checked that each finding was an object but said nothing
    about its field values — the same gap `counts` had, one level deeper. Both
    entrances (`write_result` and adoption) share this validator.
    """

    def _record(self, finding):
        return {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": "CR-1",
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": {"red": 1, "yellow": 0},
            "findings": [finding],
        }

    @pytest.mark.parametrize("bad", [{"k": "v"}, ["v"], (1, 2)])
    def test_a_container_field_is_rejected(self, bad):
        from sage_lib import results

        errs = results.validate_result(self._record({"file": "a.py", "x": bad}))
        assert any("findings[0].x must be a string" in e for e in errs), errs

    @pytest.mark.parametrize("good", ["s", None])
    def test_prose_fields_pass(self, good):
        from sage_lib import results

        assert results.validate_result(self._record({"file": "a.py", "x": good})) == []

    @pytest.mark.parametrize("bad", [3, 3.5, True])
    def test_a_number_in_a_prose_field_is_rejected(self, bad):
        """`line` is the only numeric field. Every other one is rendered through
        `html.escape()`, which raises on a non-string -- so a numeric `snippet` used
        to be adopted and then crash report generation, leaving a COMPLETED run with
        no report."""
        from sage_lib import results

        errs = results.validate_result(self._record({"file": "a.py", "snippet": bad}))
        assert any("findings[0].snippet must be a string" in e for e in errs), errs

    @pytest.mark.parametrize("good", [3, 3.5, None])
    def test_line_still_takes_a_number(self, good):
        from sage_lib import results

        assert results.validate_result(self._record({"file": "a.py", "line": good})) == []

    def test_write_result_refuses_the_record(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        with pytest.raises(ValueError, match="must be a string"):
            results.write_result(
                self._record({"file": "a.py", "evidence": {"k": "s"}}), tmp_path, "run-n1"
            )


class TestFindingLineMustBeANumber:
    """The boundary enforces `line` as a number, so no exemption rests on it."""

    def _record(self, line):
        return {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": "CR-1",
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": {"red": 1, "yellow": 0},
            "findings": [{"file": "a.py", "line": line}],
        }

    @pytest.mark.parametrize("bad", ["12", "AKIA-shaped", True, False])
    def test_a_non_numeric_line_is_rejected(self, bad):
        from sage_lib import results

        errs = results.validate_result(self._record(bad))
        assert any("findings[0].line must be a number" in e for e in errs), errs

    @pytest.mark.parametrize("good", [7, 0, 7.5, None])
    def test_a_numeric_or_absent_line_passes(self, good):
        from sage_lib import results

        assert results.validate_result(self._record(good)) == []

    def test_write_result_refuses_a_string_line(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        with pytest.raises(ValueError, match="line must be a number"):
            results.write_result(self._record("12"), tmp_path, "run-l1")


class TestNestedStringFieldsMustBeScalars:
    """Third time this class appeared: an object check that ignored its values.

    `counts` needed its values widened to numbers, finding fields needed theirs
    widened to scalars, and `phase1` / `blast_radius.rating` were still open. Their
    values are consumed as dict KEYS (`_RISK_W.get(design_risk)`,
    `_BLAST_W.get(rating)`) or with string operations (`band_override_reason
    .strip()`), so a list is unhashable / has no `.strip()` and killed report
    generation after the review had already been paid for.

    `phase1` is checked by SHAPE — because naming the fields is exactly what let
    this recur twice. `blast_radius` cannot take that rule: `signals` is
    legitimately a nested object in the schema.

    The phase1 rule was later tightened from "any scalar" to "a string" (see
    TestPhase1ValuesMustBeStrings): every field in the worker's contract is text,
    and a number passed validation and then crashed the code that renders it.
    """

    def _record(self, **over):
        r = {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": "CR-1",
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "blast_radius": {"rating": "SMALL", "signals": {"sensitive_hits": [], "loc_added": 0}},
            "counts": {"red": 0, "yellow": 0},
            "findings": [],
        }
        r.update(over)
        return r

    @pytest.mark.parametrize(
        "field",
        [
            "design_risk",
            "gate_verdict",
            "criticality",
            "band_override_reason",
            "problem",
            "solution_assessment",
        ],
    )
    @pytest.mark.parametrize("bad", [[], {}, ["x"]])
    def test_a_non_scalar_phase1_value_is_rejected(self, field, bad):
        from sage_lib import results

        p1 = {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"}
        p1[field] = bad
        errs = results.validate_result(self._record(phase1=p1))
        assert any(f"phase1.{field} must be a string" in e for e in errs), errs

    def test_a_non_string_rating_is_rejected(self):
        from sage_lib import results

        errs = results.validate_result(self._record(blast_radius={"rating": [], "signals": {}}))
        assert any("blast_radius.rating must be a string" in e for e in errs), errs

    def test_blast_radius_signals_may_stay_nested(self):
        """The schema nests `signals`; the guard must not reject a valid record."""
        from sage_lib import results

        assert results.validate_result(self._record()) == []

    def test_scalar_phase1_values_pass(self):
        from sage_lib import results

        assert (
            results.validate_result(
                self._record(
                    phase1={
                        "gate_verdict": "PASS",
                        "design_risk": "low",
                        "criticality": "low",
                        "design_headline": None,
                    }
                )
            )
            == []
        )

    def test_the_validator_itself_does_not_crash_on_a_bad_verdict(self):
        """It REPORTS a malformed record; it must never raise on one.

        `gate_verdict not in VALID_VERDICTS` tests membership of a SET, so an
        unhashable value raised TypeError inside validate_result — the function
        whose whole job is refusing malformed records died on one. The shape check
        now runs first, so every later check sees a scalar.
        """
        from sage_lib import results

        bad_verdicts: list[typing.Any] = [[], {}, ["PASS"]]
        for bad in bad_verdicts:
            errs = results.validate_result(
                self._record(
                    phase1={"gate_verdict": bad, "design_risk": "low", "criticality": "low"}
                )
            )
            assert any("phase1.gate_verdict must be a string" in e for e in errs), errs

    def test_the_scoring_lookup_really_does_raise(self):
        """The premise: an unhashable design_risk kills focus_score."""
        from sage_lib import report

        with pytest.raises(TypeError):
            report.focus_score(
                self._record(
                    phase1={"gate_verdict": "PASS", "design_risk": [], "criticality": "low"}
                )
            )

    def test_write_result_refuses_the_record(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        with pytest.raises(ValueError, match="phase1.design_risk must be a string"):
            results.write_result(
                self._record(
                    phase1={"gate_verdict": "PASS", "design_risk": [], "criticality": "low"}
                ),
                tmp_path,
                "run-p35",
            )


class TestPhase1ValuesMustBeStrings(unittest.TestCase):
    """Phase-one values are text by contract, and every reader consumes them as text.

    Accepting "any scalar" let a number through validation and then crash the code
    that renders it: `classify()` calls `.strip()` on `band_override_reason`, and the
    HTML renderer calls `html.escape()` on `gate_verdict`. Every field in the
    worker's phase1 contract is text — gate_verdict, design_risk, criticality,
    design_headline, problem, why_it_matters, solution_assessment — so a numeric
    value is malformed, and refusing the record is the fail-closed direction: it is
    re-reviewed rather than rendered half-broken.
    """

    def _record(self, **phase1):
        p1 = {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"}
        p1.update(phase1)
        return {
            "schema": "code-review-sage-result",
            "version": 1,
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "change_id": "CR-1",
            "blast_radius": {"rating": "SMALL", "signals": {}},
            "counts": {"red": 0, "yellow": 0},
            "findings": [],
            "phase1": p1,
        }

    def test_a_string_phase1_record_is_accepted(self):
        from sage_lib import results

        self.assertEqual(results.validate_result(self._record()), [])

    def test_numeric_phase1_value_is_refused(self):
        from sage_lib import results

        errs = results.validate_result(self._record(band_override_reason=42))
        self.assertTrue(any("must be a string" in e for e in errs), errs)

    def test_bool_phase1_value_is_refused(self):
        """bool is an int subclass, so it must not slip through as a number."""
        from sage_lib import results

        errs = results.validate_result(self._record(criticality=True))
        self.assertTrue(any("must be a string" in e for e in errs), errs)

    def test_numeric_gate_verdict_is_refused(self):
        """A numeric gate_verdict must not validate cleanly.

        Behind an isinstance() guard alone, a numeric gate_verdict is neither
        rejected as a shape nor checked against VALID_VERDICTS — it reaches
        `html.escape()` in the renderer, which raises.
        """
        from sage_lib import results

        errs = results.validate_result(self._record(gate_verdict=5))
        self.assertTrue(errs, "a numeric gate_verdict must not validate")

    def test_none_stays_allowed(self):
        """An optional field present as null is still a valid record."""
        from sage_lib import results

        self.assertEqual(results.validate_result(self._record(problem=None)), [])
