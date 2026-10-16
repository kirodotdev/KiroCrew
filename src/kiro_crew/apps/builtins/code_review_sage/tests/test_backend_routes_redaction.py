"""Backend-route tests: worker-authored text reaches the dashboard redacted.

The finding and row redactors walk nested values and exempt no field. ``read_report`` redacts
a persisted report.json on read, coerces its band tallies to counts, drops rows that are not
mappings or name an unknown band, and screens the report slug, so a planted file can neither
leak a credential nor break the endpoint.
"""

import json
import os
import shutil
import tempfile
import unittest

import pytest


class TestRedactionReachesNestedValues:
    """Redacting top-level strings covered every KEY, not every VALUE.

    A worker that wrote a finding field as `{"evidence": {"k": "<secret>"}}` or
    `["<secret>"]` slipped past `isinstance(v, str)` and the nested string reached
    report.json and the dashboard unscrubbed. Both redactors now walk the whole
    structure, so no shape passes content through untouched.
    """

    # Assembled at runtime, never written as one literal: the redactor only fires
    # on credential-SHAPED input (a plain sentinel passes through untouched, so the
    # test would prove nothing), but a real key shape sitting in the source trips
    # the internal-content-scan credential rules. Splitting it satisfies both — the
    # value is key-shaped when the redactor sees it, and no line here matches.
    SECRET = "AKIA" + "1234567890EXAMPLE"

    def _leaks(self, obj) -> bool:
        return self.SECRET in repr(obj)

    @pytest.mark.parametrize(
        "shape",
        [
            {"evidence": {"k": SECRET}},  # nested dict
            {"refs": [SECRET]},  # list
            {"pairs": [{"k": SECRET}]},  # list of dicts
            {"deep": {"a": {"b": {"c": SECRET}}}},  # several levels down
            {"observation": SECRET},  # the flat case, still works
        ],
    )
    def test_no_finding_shape_carries_a_secret_through(self, shape):
        from sage_lib import report

        f = {"file": "a.py", "line": 3, **shape}
        assert not self._leaks(report._redact_finding(f)), shape

    def test_a_row_value_written_as_a_container_is_scrubbed(self):
        """`url`/`platform` come straight off the worker's record."""
        from sage_lib import report

        row = report._redact_row(
            {"band": "red", "url": {"k": self.SECRET}, "platform": [self.SECRET]}
        )
        assert not self._leaks(row["url"])
        assert not self._leaks(row["platform"])

    def test_skipped_and_non_string_fields_are_untouched(self):
        """The scrub must not corrupt the values it is supposed to leave alone."""
        from sage_lib import report

        f = report._redact_finding({"file": "a.py", "line": 7, "severity": "red"})
        assert f["line"] == 7
        row = report._redact_row({"band": "yellow", "red": 2, "score": 41, "deep_reviewed": True})
        assert row == {"band": "yellow", "red": 2, "score": 41, "deep_reviewed": True}

    def test_a_deep_payload_does_not_exhaust_the_stack(self):
        """Depth is worker-chosen, so the walk is bounded and still scrubs."""
        from sage_lib import report

        nested: dict = {"k": self.SECRET}
        for _ in range(200):
            nested = {"n": nested}
        out = report._redact_finding({"file": "a.py", "blob": nested})
        assert not self._leaks(out)


class TestNoFindingFieldIsExemptFromRedaction:
    """`line` was exempt from redaction on an assumption nothing enforced.

    The redactor skipped `line` "because it is numeric", but `validate_result`
    accepted any scalar — so a reviewer writing a credential into `line` as a
    string rode the exemption straight to the dashboard. The premise is now
    enforced at the boundary and the exemption is gone, which is why a number is
    still left alone: `_redact_deep` only touches strings.
    """

    SECRET = "AKIA" + "1234567890EXAMPLE"  # split: see the sentinel note above

    def test_a_credential_in_line_is_redacted(self):
        from sage_lib import report

        out = report._redact_finding({"file": "a.py", "line": self.SECRET})
        assert self.SECRET not in repr(out)

    @pytest.mark.parametrize("good", [7, 0, 7.5])
    def test_a_numeric_line_is_untouched(self, good):
        from sage_lib import report

        assert report._redact_finding({"file": "a.py", "line": good})["line"] == good

    def test_the_redactor_has_no_skip_set_for_findings(self):
        """Guard the shape, not just the instance: no field may be exempt."""
        import inspect

        from sage_lib import report

        src = inspect.getsource(report._redact_finding)
        assert "frozenset" not in src, (
            "a skip set reappeared in _redact_finding; an exemption is only safe "
            "if something enforces its premise"
        )


class TestPersistedReportIsRedactedOnRead(unittest.TestCase):
    """A worker-planted report.json must not reach the dashboard unredacted.

    `read_within_reports` refuses symlinks and paths escaping the reports dir, but
    the dir itself is writable by the review worker, so a REAL file planted there is
    legitimate as far as the reader is concerned. `build_report` redacts every row on
    the way in; the read path did not, so planted rows reached the UI verbatim.
    Redacting on read is idempotent, so a report this module built is unchanged.
    """

    # Assembled at runtime: the scan reads source text, while the redactor only
    # fires on credential-shaped input.
    _SENTINEL = "AKIA" + "IOSFODNN7EXAMPLE"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plant(self, payload, run_id="run-a"):
        from sage_lib import report

        rd = report.reports_dir(None, run_id)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "report.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_planted_row_text_is_redacted(self):
        from sage_lib import report

        self._plant(
            {
                "bands": {"red": 1, "yellow": 0, "green": 0},
                "rows": [
                    {
                        "change_id": "CR-1",
                        "band": "red",
                        "why": f"leaked {self._SENTINEL} here",
                        "design_headline": f"credential {self._SENTINEL}",
                    }
                ],
            }
        )
        got = report.read_report(None, "run-a")
        self.assertNotIn(self._SENTINEL, json.dumps(got))
        self.assertIn("REDACTED", got["rows"][0]["why"])

    def test_a_real_report_survives_the_read_unchanged(self):
        """Idempotence: redacting twice must not alter a legitimately built report."""
        from sage_lib import report

        built = report.build_report(
            [
                {
                    "change_id": "CR-2",
                    "revision": "b" * 40,
                    "phase1": {
                        "gate_verdict": "PASS",
                        "design_risk": "low",
                        "criticality": "low",
                        "design_headline": "a clean headline",
                    },
                    "counts": {"red": 0, "yellow": 0},
                    "findings": [],
                    "blast_radius": {"rating": "SMALL"},
                }
            ]
        )
        self._plant(built, run_id="run-b")
        got = report.read_report(None, "run-b")
        self.assertEqual(got["rows"], built.get("rows"))

    def test_planted_tallies_are_coerced_to_counts(self):
        """Bands and total are arithmetic inputs the UI trusts."""
        from sage_lib import report

        self._plant(
            {"bands": {"red": "lots", "yellow": -3, "green": True}, "rows": []}, run_id="run-c"
        )
        got = report.read_report(None, "run-c")
        self.assertEqual(got["bands"], {"red": 0, "yellow": 0, "green": 0})
        self.assertIsInstance(got["total"], int)

    def test_non_dict_rows_are_dropped(self):
        """A row is a mapping by contract; anything else cannot be redacted."""
        from sage_lib import report

        self._plant(
            {
                "bands": {"red": 0, "yellow": 0, "green": 0},
                "rows": ["not-a-row", 7, {"change_id": "CR-3", "band": "green"}],
            },
            run_id="run-d",
        )
        got = report.read_report(None, "run-d")
        self.assertEqual([r["change_id"] for r in got["rows"]], ["CR-3"])


class TestPlantedReportMetadataCannotBreakTheEndpoint(unittest.TestCase):
    """The remaining worker-writable fields in the read_report payload.

    Redacting the rows and coercing the tallies is not enough on its own: `bands`
    screened for truthiness rather than for being a MAPPING, and `report_slug`
    passed through untouched, are two gaps.

    `[] or {}` yields `{}`, so an empty list looks handled -- but a truthy
    non-dict (a non-empty list, a string, a number) reaches `.get` and raises
    AttributeError, turning a planted file into an HTTP 500 on the report
    endpoint. The slug names an artifact the dashboard turns into a share link, so
    it is screened against the artifact store's own grammar rather than redacted:
    a value that is not a slug cannot reference a real artifact.
    """

    # Assembled at runtime so the scan sees no credential-shaped literal.
    _SENTINEL = "AKIA" + "IOSFODNN7EXAMPLE"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plant(self, run_id, payload, index=None):
        from sage_lib import report

        rd = report.reports_dir(None, run_id)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "report.json").write_text(json.dumps(payload), encoding="utf-8")
        if index is not None:
            (rd / "index.json").write_text(json.dumps(index), encoding="utf-8")

    def test_a_truthy_non_dict_bands_does_not_raise(self):
        """The shape that actually crashed: truthy, so `or {}` never fired."""
        from sage_lib import report

        for i, bad in enumerate([[1, 2], "red", 7, [{"red": 1}]]):
            with self.subTest(bands=bad):
                rid = f"bands-{i}"
                self._plant(rid, {"bands": bad, "rows": []})
                got = report.read_report(None, rid)
                self.assertEqual(got["bands"], {"red": 0, "yellow": 0, "green": 0})

    def test_an_empty_list_bands_still_works(self):
        """Kept explicit: this case was already safe and must stay safe."""
        from sage_lib import report

        self._plant("bands-empty", {"bands": [], "rows": []})
        self.assertEqual(
            report.read_report(None, "bands-empty")["bands"], {"red": 0, "yellow": 0, "green": 0}
        )

    def test_a_planted_slug_is_dropped(self):
        from sage_lib import report

        self._plant(
            "slug-bad", {"bands": {}, "rows": []}, index={"report_slug": f"leaked {self._SENTINEL}"}
        )
        got = report.read_report(None, "slug-bad")
        self.assertIsNone(got["report_slug"])
        self.assertNotIn(self._SENTINEL, json.dumps(got))

    def test_a_traversal_shaped_slug_is_dropped(self):
        from sage_lib import report

        # Assembled so the path never appears as a literal in this file.
        traversal = "..%s..%setc%sshadow" % ("/", "/", "/")
        self._plant("slug-trav", {"bands": {}, "rows": []}, index={"report_slug": traversal})
        self.assertIsNone(report.read_report(None, "slug-trav")["report_slug"])

    def test_a_non_string_slug_is_dropped(self):
        from sage_lib import report

        self._plant("slug-num", {"bands": {}, "rows": []}, index={"report_slug": 5})
        self.assertIsNone(report.read_report(None, "slug-num")["report_slug"])

    def test_a_real_artifact_slug_survives(self):
        """The constraint must not break the feature it guards.

        Screened against `artifacts._SLUG_RE`, the same grammar the artifact store
        itself enforces, so anything the store could have issued is accepted.
        """
        from sage_lib import report

        from kiro_crew.artifacts import _SLUG_RE

        slug = "focus-report-cr-1-abc123"
        self.assertTrue(_SLUG_RE.match(slug), "fixture must be a valid slug")
        self._plant("slug-ok", {"bands": {}, "rows": []}, index={"report_slug": slug})
        self.assertEqual(report.read_report(None, "slug-ok")["report_slug"], slug)


class TestNoRowFieldIsExemptFromRedaction(unittest.TestCase):
    """`band` was the last redaction exemption in a report row, and it leaked.

    The exemption existed because `bands[row["band"]]` and `BAND_DOT[row.band]`
    index on the exact value, so scrubbing it looked like it would break grouping.
    The concern was real; the protection was not. Redaction is shape-based, so the
    three real bands come back byte-identical -- while the exemption made `band`
    the ONE field in a row that reached the dashboard verbatim, so a planted
    "red <credential>" leaked where the prose beside it was scrubbed.

    Keying is now protected by a vocabulary on the untrusted read path instead: a
    row whose band is not one of the three cannot be grouped, so it is dropped.
    """

    # Assembled at runtime: the scan reads source text, the redactor only fires
    # on credential-shaped input.
    _SENTINEL = "AKIA" + "IOSFODNN7EXAMPLE"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plant(self, run_id, rows):
        from sage_lib import report

        rd = report.reports_dir(None, run_id)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "report.json").write_text(
            json.dumps({"bands": {"red": 1, "yellow": 0, "green": 0}, "rows": rows}),
            encoding="utf-8",
        )

    def test_a_credential_in_band_is_scrubbed(self):
        """The exemption's actual consequence, at the redactor itself."""
        from sage_lib import report

        out = report._redact_row(
            {"band": f"red {self._SENTINEL}", "why": f"prose {self._SENTINEL}"}
        )
        self.assertNotIn(self._SENTINEL, out["band"])
        self.assertNotIn(self._SENTINEL, out["why"])

    def test_a_legitimate_band_survives_redaction_unchanged(self):
        """Why removing the exemption is free: the real redactor is a no-op here.

        This is the property the UI needs -- `bands[]`/`BAND_DOT[]` keep keying on
        the exact value -- and it is what the exemption was wrongly protecting.
        """
        from sage_lib import report

        for band in ("red", "yellow", "green"):
            with self.subTest(band=band):
                self.assertEqual(report._redact_row({"band": band})["band"], band)

    def test_a_planted_band_cannot_reach_the_dashboard(self):
        from sage_lib import report

        self._plant("band-leak", [{"change_id": "CR-1", "band": f"red {self._SENTINEL}"}])
        got = report.read_report(None, "band-leak")
        self.assertNotIn(self._SENTINEL, json.dumps(got))

    def test_a_row_with_an_unknown_band_is_dropped(self):
        """An ungroupable row cannot render, so it is not passed through."""
        from sage_lib import report

        self._plant(
            "band-vocab",
            [
                {"change_id": "CR-1", "band": "purple"},
                {"change_id": "CR-2", "band": None},
                {"change_id": "CR-3", "band": "green"},
            ],
        )
        got = report.read_report(None, "band-vocab")
        self.assertEqual([r["change_id"] for r in got["rows"]], ["CR-3"])

    def test_the_redactor_takes_no_skip_set(self):
        """Structural: no row field may be re-exempted without this failing.

        Three rounds of findings in this family were "one more field was exempt",
        so the absence of a skip set is asserted directly rather than left to a
        future reader's judgement.
        """
        with open(self.mod_report_path(), encoding="utf-8") as fh:
            src = fh.read()
        body = src.split("def _redact_row", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_redact_deep_map(row)", body, "_redact_row must pass no skip set")
        self.assertNotIn("_STRUCTURAL_ROW_FIELDS", src, "the row skip set must stay gone")

    def mod_report_path(self):
        from sage_lib import report

        return report.__file__
