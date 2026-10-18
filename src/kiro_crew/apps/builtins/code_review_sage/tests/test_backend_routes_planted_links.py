"""Backend-route tests: no read or write follows a link a worker planted.

The reviewer worker has file tools and shares the staging dir, a run's results dir, the
reports dir and the run registry's dir, so every path there is an untrusted target.
Adoption, publishing, the report writes, the run registry and the result and report reads
each refuse or replace a planted link instead of following it. All but the registry pin that a
real file still reads or writes normally; the registry's normal path is pinned in
``test_backend_routes.py``.
"""

import json
import os
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import pytest
from sage_lib import report as R
from sage_lib import results, store

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.code_review_sage.tests.backend_routes_helpers import (
    _load_routes_module,
)
from kiro_crew.apps.builtins.code_review_sage.tests.fixtures import SYMLINKS_OK


class TestAdoptionRefusesAPlantedLink:
    """The reviewer worker owns the shared dir and has file tools.

    ``is_file()`` follows symlinks, and ``os.replace`` moves the LINK, so a link
    planted where a record belongs lands in the run dir intact unless refused —
    and ``read_result`` dereferences it with a plain read. Adoption must never carry a
    link across, and must not leave one behind to retry.
    """

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_symlink_is_refused_and_removed(self, tmp_path):

        store.ensure_layout(tmp_path)
        secret = tmp_path / "outside-secret.txt"
        secret.write_text("SENSITIVE", encoding="utf-8")

        shared = results.results_dir(tmp_path, None)
        shared.mkdir(parents=True, exist_ok=True)
        planted = shared / f"{results.safe_change_id('CR-1')}.json"
        planted.symlink_to(secret)

        assert results.adopt_from_shared("CR-1", tmp_path, "run-1") is False
        # Nothing was adopted...
        assert results.read_result("CR-1", tmp_path, "run-1") is None
        # ...the link is gone, so the same plant cannot be retried...
        assert not planted.exists() and not planted.is_symlink()
        # ...and the target itself was left alone.
        assert secret.read_text(encoding="utf-8") == "SENSITIVE"

    def test_a_real_record_is_still_adopted_as_a_regular_file(self, tmp_path):

        store.ensure_layout(tmp_path)
        shared = results.results_dir(tmp_path, None)
        shared.mkdir(parents=True, exist_ok=True)
        (shared / f"{results.safe_change_id('CR-2')}.json").write_text(
            json.dumps(
                {
                    "schema": "code-review-sage-result",
                    "version": 1,
                    "change_id": "CR-2",
                    "platform": "github",
                    "repo_identity": "github.com/o/r",
                    "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
                    "counts": {"red": 0, "yellow": 1},
                }
            ),
            encoding="utf-8",
        )

        assert results.adopt_from_shared("CR-2", tmp_path, "run-1") is True
        got = results.read_result("CR-2", tmp_path, "run-1")
        assert got and got["change_id"] == "CR-2"
        dst = results.result_path("CR-2", tmp_path, "run-1")
        assert dst.is_file() and not dst.is_symlink()
        # The shared copy is consumed, as before.
        assert not (shared / f"{results.safe_change_id('CR-2')}.json").exists()


class TestPublishRefusesAPlantedDestinationLink:
    """The shared dir is worker-writable, so its paths are untrusted targets.

    Publishing wrote with `dst.write_bytes(...)`, which follows a symlink at the
    destination. The worker can plant one there, so the write landed on whatever it
    pointed at. Renaming over the name instead destroys the plant.
    """

    def _record(self, cid="CR-1"):
        return {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": cid,
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": {"red": 0, "yellow": 1},
        }

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_planted_link_is_replaced_not_followed(self, tmp_path):

        store.ensure_layout(tmp_path)
        results.write_result(self._record(), tmp_path, "run-1")

        outside = tmp_path / "outside-secret.txt"
        outside.write_text("SENSITIVE", encoding="utf-8")

        shared = results.results_dir(tmp_path, None)
        shared.mkdir(parents=True, exist_ok=True)
        planted = shared / f"{results.safe_change_id('CR-1')}.json"
        planted.symlink_to(outside)

        assert results.publish_to_shared("CR-1", tmp_path, "run-1") is True

        # The target is untouched — the write did not follow the link.
        assert outside.read_text(encoding="utf-8") == "SENSITIVE"
        # And the shared path is now a real file holding the record.
        assert planted.is_file() and not planted.is_symlink()
        assert json.loads(planted.read_text(encoding="utf-8"))["change_id"] == "CR-1"

    def test_publishing_still_works_with_no_link_present(self, tmp_path):

        store.ensure_layout(tmp_path)
        results.write_result(self._record("CR-2"), tmp_path, "run-1")
        assert results.publish_to_shared("CR-2", tmp_path, "run-1") is True
        shared = results.results_dir(tmp_path, None)
        got = json.loads(
            (shared / f"{results.safe_change_id('CR-2')}.json").read_text(encoding="utf-8")
        )
        assert got["change_id"] == "CR-2"

    def test_no_temp_files_are_left_behind(self, tmp_path):

        store.ensure_layout(tmp_path)
        results.write_result(self._record("CR-3"), tmp_path, "run-1")
        assert results.publish_to_shared("CR-3", tmp_path, "run-1") is True
        shared = results.results_dir(tmp_path, None)
        assert not list(shared.glob(".publish-*")), "publish left a temp file behind"


class TestPublishRefusesAPlantedSourceLink:
    """The RUN results dir is worker-writable too, so the source is untrusted.

    Its sibling above covers the write: a link planted at the DESTINATION is
    replaced rather than followed. This covers the read. A worker that swaps its
    own finished record for a link to something outside the sandbox would
    otherwise have the linked bytes copied into the SHARED staging dir, which
    every worker can read. Same function, same trust boundary, opposite
    direction.
    """

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_symlinked_record_is_not_published(self, tmp_path):

        store.ensure_layout(tmp_path)
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("SENSITIVE", encoding="utf-8")

        # The worker plants a link where its own record belongs.
        src = results.result_path("CR-9", tmp_path, "run-9")
        src.parent.mkdir(parents=True, exist_ok=True)
        src.symlink_to(outside)

        assert results.publish_to_shared("CR-9", tmp_path, "run-9") is False

        shared = results.results_dir(tmp_path, None)
        landed = shared / f"{results.safe_change_id('CR-9')}.json"
        if landed.exists():
            assert "SENSITIVE" not in landed.read_text(encoding="utf-8")
        # A refused publish leaves no staging residue behind either.
        assert list(shared.glob(".publish-*")) == []

    def test_a_hardlinked_record_is_not_published(self, tmp_path):
        """The nolink guard also rejects a shared inode, not just a symlink."""

        store.ensure_layout(tmp_path)
        outside = tmp_path / "outside-hard.txt"
        outside.write_text("SENSITIVE", encoding="utf-8")

        src = results.result_path("CR-10", tmp_path, "run-10")
        src.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(outside, src)
        except OSError:  # pragma: no cover - platform without links
            pytest.skip("hardlinks unavailable here")

        assert results.publish_to_shared("CR-10", tmp_path, "run-10") is False


class TestReportWritesRefusePlantedLinks:
    """Report output must not be written through a worker-planted symlink.

    The reports dir is reachable by the review worker, so every output name in
    `generate()` -- focus-report.html, rows.json, report.json, index.json -- is an
    untrusted path. `write_text` followed a link planted at any of them and
    overwrote the linked host file, and the `os.chmod` after the write followed it
    too. All four now go through an atomic temp+rename, which swaps the name
    without following a link.
    """

    def _report(self):
        return {
            "rows": [
                {"band": "red", "title": "t", "change_id": "CR-1", "findings": []},
                {"band": "green", "title": "g", "change_id": "CR-2", "findings": []},
            ],
            "bands": {"red": 1, "yellow": 0, "green": 1},
            "generated_at": "2026-01-01T00:00:00Z",
        }

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    @pytest.mark.parametrize(
        "name",
        [
            "focus-report.html",
            "rows.json",
            "report.json",
            "index.json",
        ],
    )
    def test_a_planted_link_is_replaced_not_followed(self, tmp_path, name):

        store.ensure_layout(tmp_path)
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("SENSITIVE", encoding="utf-8")

        rd = R.reports_dir(tmp_path, "run-r1")
        rd.mkdir(parents=True, exist_ok=True)
        planted = rd / name
        planted.symlink_to(outside)

        R.write_outputs(self._report(), "<html>body</html>", tmp_path, None, "run-r1")

        # The link target is untouched -- the write did not follow it.
        assert outside.read_text(encoding="utf-8") == "SENSITIVE"
        # And the output is now a real file, not a link.
        assert planted.is_file() and not planted.is_symlink()

    def test_outputs_are_private_and_leave_no_temp_behind(self, tmp_path):

        store.ensure_layout(tmp_path)
        R.write_outputs(self._report(), "<html>body</html>", tmp_path, None, "run-r2")

        rd = R.reports_dir(tmp_path, "run-r2")
        for name in ("focus-report.html", "rows.json", "report.json", "index.json"):
            p = rd / name
            assert p.is_file(), f"{name} was not written"
            # The temp file is chmod'ed before it takes the real name, so the
            # mode must hold on the final path with no separate chmod step.
            # Windows expresses the same owner-only lockdown as a DACL, which
            # st_mode never reflects (it always reports 0o666), so the mode
            # bits are only observable on POSIX.
            if platform_compat.IS_POSIX:
                assert oct(p.stat().st_mode)[-3:] == "600", f"{name} is not 0600"
        assert list(rd.glob("*.tmp")) == [], "a staging temp file survived"


class TestTheRunRegistryRefusesAPlantedLink(unittest.TestCase):
    """The registry's DIRECTORY, not just its guarded write.

    `_write_runs` publishes through the shared link-refusing helper, but it has
    to create the directory first, and a bare `mkdir(parents=True)` creates
    THROUGH a link it meets -- so by the time the write's own refusal runs, the
    tree already exists where the planter wants it. The review worker shares this
    directory and is prompt-injectable, which is why the order matters here.
    """

    def setUp(self):
        self.routes = _load_routes_module()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_link_above_the_registry_takes_no_bytes(self):
        impostor = self.tmp / "impostor"
        impostor.mkdir()
        planted = self.tmp / "data"
        try:
            planted.symlink_to(impostor)
        except (OSError, NotImplementedError):
            self.skipTest("planting the attack needs symlink creation")
        target = planted / "runs" / "runs.json"

        with (
            unittest.mock.patch.object(self.routes, "_runs_file", lambda: target),
            unittest.mock.patch("kiro_crew.config.paths.data_home", lambda: str(self.tmp)),
        ):
            with self.assertRaises(OSError):
                self.routes._write_runs("[]")

        self.assertEqual(
            list(impostor.iterdir()), [], "the tree was built inside the link's target"
        )


class TestReportsDirReadsDoNotFollowAPlant:
    """The reports-dir READS must not follow a plant, just as the WRITES do not.

    The dir is reachable by the review worker, so an unguarded read of a symlink at
    `index.json` or `focus-report.html` follows it and its contents flow onward —
    into a rendered report, or into a shareable dashboard artifact.
    """

    def _plant(self, tmp_path, name, body):
        from sage_lib import report

        rd = report.reports_dir(tmp_path, "run-r1")
        rd.mkdir(parents=True, exist_ok=True)
        secret = tmp_path / "outside-secret.txt"
        secret.write_text(body, encoding="utf-8")
        (rd / name).symlink_to(secret)
        return rd / name

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_planted_html_link_is_not_read(self, tmp_path):
        from sage_lib import report

        link = self._plant(tmp_path, "focus-report.html", "TOP-SECRET-BODY")
        assert link.is_file()  # the link resolves — it just must not be read
        assert report.read_within_reports(link, tmp_path, "run-r1") is None

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_planted_index_link_is_not_read(self, tmp_path):
        from sage_lib import report

        link = self._plant(tmp_path, "index.json", json.dumps({"report_slug": "stolen"}))
        assert report.read_within_reports(link, tmp_path, "run-r1") is None

    def test_a_real_file_still_reads(self, tmp_path):
        from sage_lib import report

        rd = report.reports_dir(tmp_path, "run-r2")
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "index.json").write_text('{"report_slug": "ok"}', encoding="utf-8")
        got = report.read_within_reports(rd / "index.json", tmp_path, "run-r2")
        assert json.loads(got or "{}")["report_slug"] == "ok"

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_read_report_refuses_a_planted_report_json(self, tmp_path):
        """The consumer, not just the helper: a plant renders as no report."""
        from sage_lib import report

        self._plant(tmp_path, "report.json", json.dumps({"rows": ["leak"]}))
        assert report.read_report(tmp_path, "run-r1") is None

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_set_report_slug_does_not_merge_a_planted_index(self, tmp_path):
        from sage_lib import report

        self._plant(tmp_path, "index.json", json.dumps({"secret": "leak"}))
        idx = report.set_report_slug("s1", tmp_path, "run-r1")
        assert idx == {"report_slug": "s1"}, idx
        rd = report.reports_dir(tmp_path, "run-r1")
        assert not (rd / "index.json").is_symlink()  # the plant was replaced
        assert "leak" not in (rd / "index.json").read_text(encoding="utf-8")


class TestResultReadsDoNotFollowAPlantedLink:
    """Runs are concurrent, so another live worker can reach a run's results dir.

    A prompt-injected reviewer replaces another run's record with a symlink; a
    plain read dereferences it and the victim's report and posted payload are built
    from attacker-chosen JSON filed under the victim's change id. `publish_to_shared`
    and `adopt_from_shared` already guarded their copies — the readers did not.
    """

    def _plant(self, tmp_path, run_id, change_id, payload):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        rd = results.results_dir(tmp_path, run_id)
        rd.mkdir(parents=True, exist_ok=True)
        target = tmp_path / "attacker.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        (rd / f"{results.safe_change_id(change_id)}.json").symlink_to(target)
        return rd

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_read_result_refuses_a_planted_record(self, tmp_path):
        from sage_lib import results

        self._plant(tmp_path, "victim", "CR-1", {"change_id": "ATTACKER"})
        assert results.read_result("CR-1", tmp_path, "victim") is None

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_list_results_skips_a_planted_record(self, tmp_path):
        from sage_lib import results

        self._plant(tmp_path, "victim", "CR-1", {"change_id": "ATTACKER"})
        assert results.list_results(tmp_path, "victim") == []

    def test_a_real_record_still_reads(self, tmp_path):
        """The guard must not break the normal path."""
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        rec = {
            "schema": "code-review-sage-result",
            "version": 1,
            "change_id": "CR-2",
            "platform": "github",
            "repo_identity": "github.com/o/r",
            "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
            "counts": {"red": 0, "yellow": 0},
            "findings": [],
        }
        results.write_result(rec, tmp_path, "ok-run")
        got = results.read_result("CR-2", tmp_path, "ok-run")
        assert got is not None and got["change_id"] == "CR-2"
        assert [r["change_id"] for r in results.list_results(tmp_path, "ok-run")] == ["CR-2"]

    def test_a_non_object_record_is_refused(self, tmp_path):
        """Consumers index with .get(); a list or scalar must not reach them."""
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        rd = results.results_dir(tmp_path, "odd")
        rd.mkdir(parents=True, exist_ok=True)
        (rd / f"{results.safe_change_id('CR-3')}.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert results.read_result("CR-3", tmp_path, "odd") is None

    def test_a_missing_record_is_still_none(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        assert results.read_result("CR-NONE", tmp_path, "empty") is None

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_the_reviewed_index_is_guarded_too(self, tmp_path):
        """It decides which PRs count as reviewed, so a swap suppresses reviews."""
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        target = tmp_path / "fake-index.json"
        target.write_text(json.dumps({"GH-o-r-1": {"head_sha": "x"}}), encoding="utf-8")
        p = results.reviewed_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            p.unlink()
        p.symlink_to(target)
        assert results.read_reviewed(tmp_path) == {}

    def test_a_real_reviewed_index_still_loads(self, tmp_path):
        from sage_lib import results, store

        store.ensure_layout(tmp_path)
        results.write_reviewed({"GH-o-r-9": {"head_sha": "abc"}}, tmp_path)
        assert results.read_reviewed(tmp_path) == {"GH-o-r-9": {"head_sha": "abc"}}
