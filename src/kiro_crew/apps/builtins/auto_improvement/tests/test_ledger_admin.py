"""Ledger maintenance: forget, purge, and the dead-record sweep.

Every operation here exists to UNDO a dedup decision, so the failure modes are
asymmetric and quiet. A forget that does not actually clear the dedup entry looks
like it worked and changes nothing; a purge that fires on a live record destroys the
only copy of a queued change. These tests pin both directions, plus the append-only
and torn-line guarantees that make the ledger safe to write while a run is reading
it.
"""

from __future__ import annotations

import json
import threading
import unittest.mock as mock
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import ledger_admin as la
from kiro_crew.apps.builtins.auto_improvement.backend import store

REAL_PR = "https://github.com/o/r/pull/7"


@pytest.fixture()
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
    # Pin workspace_dir == data_dir so flat test paths and the per-repo layout
    # coincide (see the note in test_finding_detail's fixture).
    monkeypatch.setattr(store, "workspace_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    store.ensure_layout()
    return tmp_path / "data"


def _write_ledger(root: Path, rows: list[dict[str, Any]]) -> None:
    root.joinpath("ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def _events(root: Path) -> list[dict[str, Any]]:
    text = root.joinpath("ledger.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _row(fp: str, status: str, **extra: Any) -> dict[str, Any]:
    return {"fp": fp, "kind": "bug", "target": "src/x.py::f", "status": status, **extra}


class TestFingerprintValidation:
    @pytest.mark.parametrize(
        "fp",
        [
            "../../../etc/passwd",
            "..",
            "a/../b",
            "sub/dir",
            "/absolute",
            "with space",
            "has.dot",
            "trailing\n",
            "",
            "-leading-dash",
            "x" * 65,
        ],
    )
    def test_unsafe_fingerprints_are_rejected(self, fp: str) -> None:
        """An ``fp`` arrives from a URL path segment and then names a file that gets
        DELETED, so the shape rule is an allowlist and a dot is not in it — a
        suffix-swap is as dangerous as a traversal here."""
        with pytest.raises(ValueError):
            la.validate_fingerprint(fp)

    @pytest.mark.parametrize("fp", ["b8cdaa6362cad173", "a" * 16, "A1_b-2", "0"])
    def test_real_fingerprints_are_accepted(self, fp: str) -> None:
        assert la.validate_fingerprint(fp) == fp

    def test_the_error_does_not_echo_the_input(self) -> None:
        """The message reaches an HTTP client; it must not reflect a crafted path or
        name an internal directory."""
        with pytest.raises(ValueError) as exc:
            la.validate_fingerprint("../../etc/shadow")
        assert "etc" not in str(exc.value)
        assert "/" not in str(exc.value)


class TestIsDeadRecord:
    def test_only_filed_records_can_be_dead(self) -> None:
        """A verdict is not garbage just because it was unwelcome — only a record
        claiming to have FILED something can be wrong about it."""
        for status in ("seen", "failed_gate", "failed_verify", "duplicate", "error", "purged"):
            assert la.is_dead_record(_row("f", status)) is False
            assert la.is_dead_record(_row("f", status, cr="")) is False

    def test_filed_with_no_reference_is_dead(self) -> None:
        assert la.is_dead_record(_row("f", "filed")) is True
        assert la.is_dead_record(_row("f", "filed", cr="")) is True
        assert la.is_dead_record(_row("f", "filed", pr="")) is True

    def test_filed_with_a_real_pull_request_is_alive(self) -> None:
        assert la.is_dead_record(_row("f", "filed", cr=REAL_PR)) is False
        assert la.is_dead_record(_row("f", "filed", pr=REAL_PR)) is False
        merge_request = "https://gitlab.com/g/p/-/merge_requests/3"
        assert la.is_dead_record(_row("f", "filed", cr=merge_request)) is False

    def test_queued_placeholder_is_not_dead(self) -> None:
        """``QUEUED:<fp>`` means the change is on disk and drafting can be retried.
        Treating it as dead would purge every locally queued change the moment the
        provider CLI was unavailable."""
        assert la.is_dead_record(_row("f", "filed", cr="QUEUED:abc123")) is False
        assert la.is_dead_record(_row("f", "filed", cr="queued:abc123")) is False

    def test_filed_with_a_non_url_reference_is_dead(self) -> None:
        for ref in ("not-a-url", "http://example.com/pull/1", "https://example.com/pull/", "TBD"):
            assert la.is_dead_record(_row("f", "filed", cr=ref)) is True

    def test_both_key_spellings_are_read(self) -> None:
        """The on-disk field is historically ``cr``; newer writers use ``pr``. Reading
        only one spelling would judge half the ledger dead."""
        assert la.pr_reference({"cr": REAL_PR}) == REAL_PR
        assert la.pr_reference({"pr": REAL_PR}) == REAL_PR
        assert la.pr_reference({"cr": "", "pr": REAL_PR}) == REAL_PR
        assert la.pr_reference({}) == ""


class TestForget:
    def test_appends_a_purged_event_and_keeps_history(self, data_home: Path) -> None:
        """Append-only: the timeline must still show what happened before the human
        intervened, so the operation adds an event rather than editing one."""
        _write_ledger(data_home, [_row("f1", "seen"), _row("f1", "failed_gate")])
        result = la.forget("f1")
        assert result == {"ok": True, "fp": "f1", "forgotten": True, "reason": "", "detail": ""}

        rows = _events(data_home)
        assert [r["status"] for r in rows] == ["seen", "failed_gate", "purged"]
        assert rows[-1]["kind"] == "bug"
        assert rows[-1]["target"] == "src/x.py::f"
        assert rows[-1]["ts"] > 0

    def test_the_purged_event_is_loadable_by_the_spine_ledger(self, data_home: Path) -> None:
        """The WHOLE POINT of forget: ``Ledger.known()`` must report the locus as
        unknown afterward. ``LedgerEntry`` is a fixed-field dataclass built as
        ``LedgerEntry(**row)``, and ``_load()`` swallows a TypeError as a torn line —
        so an event carrying an unexpected key is DROPPED, the fingerprint never
        enters the index as purged, and the locus stays deduped. The purge would look
        like it worked and change nothing."""
        from kiro_crew.apps.builtins.auto_improvement.spine import ledger as spine_ledger

        _write_ledger(data_home, [_row("f1", "failed_gate")])
        assert spine_ledger.Ledger(store.ledger_path()).known("f1") is True

        assert la.forget("f1")["ok"] is True
        reloaded = spine_ledger.Ledger(store.ledger_path())
        assert reloaded.known("f1") is False
        assert reloaded._seen["f1"].status == spine_ledger.STATUS_PURGED

    def test_status_purged_matches_the_spine_constant(self) -> None:
        """The literal here mirrors the spine's constant to avoid importing the whole
        engine on a request path. Pin them together so the mirror cannot drift."""
        from kiro_crew.apps.builtins.auto_improvement.spine import ledger as spine_ledger

        assert la.STATUS_PURGED == spine_ledger.STATUS_PURGED

    def test_unknown_fingerprint_is_refused(self, data_home: Path) -> None:
        _write_ledger(data_home, [_row("f1", "seen")])
        result = la.forget("nope")
        assert result["ok"] is False
        assert result["reason"] == "unknown_finding"
        assert _events(data_home) == [_row("f1", "seen")]

    def test_refuses_a_finding_with_a_real_pull_request(self, data_home: Path) -> None:
        """Forgetting it would let the loop rediscover the locus and open a SECOND
        pull request for a change already under review."""
        _write_ledger(data_home, [_row("f1", "filed", cr=REAL_PR)])
        result = la.forget("f1")
        assert result["ok"] is False
        assert result["reason"] == "has_pull_request"
        assert result["pr"] == REAL_PR
        assert len(_events(data_home)) == 1

    def test_allows_a_finding_whose_pull_request_never_materialized(self, data_home: Path) -> None:
        """A ``QUEUED:`` placeholder is not a live pull request, so there is nothing
        to duplicate — this is exactly the record a human wants retried."""
        _write_ledger(data_home, [_row("f1", "filed", cr="QUEUED:f1")])
        assert la.forget("f1")["ok"] is True
        assert _events(data_home)[-1]["status"] == "purged"

    def test_clears_the_reference_on_the_purged_event(self, data_home: Path) -> None:
        """The reference is precisely what is being disowned; carrying it forward would
        make the purged record look like it still filed something."""
        _write_ledger(data_home, [_row("f1", "filed", cr="QUEUED:f1")])
        la.forget("f1")
        assert _events(data_home)[-1]["cr"] == ""
        assert la.pr_reference(_events(data_home)[-1]) == ""

    def test_leaves_artifacts_alone(self, data_home: Path) -> None:
        """Unlike purge: forget is for a wrong verdict, and the evidence is what makes
        the retry possible."""
        _write_ledger(data_home, [_row("f1", "failed_gate")])
        diff = store.pr_queue_dir() / "f1.diff"
        diff.write_text("--- a\n+++ b\n", encoding="utf-8")
        la.forget("f1")
        assert diff.is_file()

    def test_rejects_an_unsafe_fingerprint_without_touching_the_ledger(
        self, data_home: Path
    ) -> None:
        _write_ledger(data_home, [_row("f1", "seen")])
        result = la.forget("../../../etc/passwd")
        assert result["ok"] is False and result["reason"] == "invalid_fingerprint"
        assert len(_events(data_home)) == 1

    def test_forget_on_a_missing_ledger_is_a_refusal_not_a_crash(self, data_home: Path) -> None:
        result = la.forget("f1")
        assert result["ok"] is False and result["reason"] == "unknown_finding"


class TestPurge:
    def test_purges_a_dead_record_and_removes_its_artifacts(self, data_home: Path) -> None:
        _write_ledger(data_home, [_row("f1", "seen"), _row("f1", "filed", cr="")])
        (store.pr_queue_dir() / "f1.diff").write_text("--- a\n", encoding="utf-8")
        (store.pr_queue_dir() / "f1.pr.md").write_text("# fix\n", encoding="utf-8")
        (store.profiles_dir() / "f1.json").write_text("{}", encoding="utf-8")
        (store.profiles_dir() / "f1.pstats").write_bytes(b"raw")
        keep = store.pr_queue_dir() / "other.diff"
        keep.write_text("--- a\n", encoding="utf-8")

        result = la.purge("f1")
        assert result["ok"] is True and result["purged"] is True
        assert sorted(result["removed"]) == [
            "pr_queue/f1.diff",
            "pr_queue/f1.pr.md",
            "profiles/f1.json",
            "profiles/f1.pstats",
        ]
        assert _events(data_home)[-1]["status"] == "purged"
        assert not (store.pr_queue_dir() / "f1.diff").exists()
        assert not (store.profiles_dir() / "f1.pstats").exists()
        assert keep.is_file()  # another finding's artifacts are untouched

    def test_refuses_a_live_record(self, data_home: Path) -> None:
        """This deletes files, so it only fires on a record that can never make
        progress. A merely unwelcome verdict keeps its evidence — use forget."""
        _write_ledger(data_home, [_row("f1", "filed", cr=REAL_PR)])
        diff = store.pr_queue_dir() / "f1.diff"
        diff.write_text("--- a\n", encoding="utf-8")

        result = la.purge("f1")
        assert result["ok"] is False and result["reason"] == "not_dead"
        assert result["pr"] == REAL_PR
        assert diff.is_file()
        assert len(_events(data_home)) == 1

    def test_refuses_a_queued_record(self, data_home: Path) -> None:
        """A queued change is still materializable, and its diff is the only copy."""
        _write_ledger(data_home, [_row("f1", "filed", cr="QUEUED:f1")])
        result = la.purge("f1")
        assert result["ok"] is False and result["reason"] == "not_dead"

    def test_refuses_a_non_filed_record(self, data_home: Path) -> None:
        _write_ledger(data_home, [_row("f1", "failed_gate")])
        result = la.purge("f1")
        assert result["ok"] is False and result["reason"] == "not_dead"

    def test_unknown_fingerprint_is_refused(self, data_home: Path) -> None:
        _write_ledger(data_home, [_row("f1", "seen")])
        result = la.purge("nope")
        assert result["ok"] is False and result["reason"] == "unknown_finding"

    def test_rejects_an_unsafe_fingerprint_before_deleting_anything(self, data_home: Path) -> None:
        """The traversal guard has to fire BEFORE the glob, because the glob is what
        selects files for unlink."""
        outside = data_home.parent / "precious.diff"
        outside.write_text("keep me", encoding="utf-8")
        result = la.purge("../precious")
        assert result["ok"] is False and result["reason"] == "invalid_fingerprint"
        assert outside.is_file()

    def test_records_the_event_before_removing_artifacts(self, data_home: Path) -> None:
        """Ordered deliberately: an event with a leftover file is a re-discoverable
        locus plus stale data, while deleted files with no event is a locus still
        blocked by dedup whose evidence is gone. Simulated by making the unlink fail."""
        _write_ledger(data_home, [_row("f1", "filed", cr="")])
        (store.pr_queue_dir() / "f1.diff").write_text("--- a\n", encoding="utf-8")

        def _boom(_self: Path) -> None:
            raise OSError("device busy")

        with mock.patch.object(Path, "unlink", _boom):
            outcome = la.purge("f1")
        assert outcome["ok"] is True  # the ledger event stands on its own
        assert outcome["removed"] == []
        assert _events(data_home)[-1]["status"] == "purged"
        assert (store.pr_queue_dir() / "f1.diff").is_file()

    def test_remove_artifacts_can_be_opted_out(self, data_home: Path) -> None:
        _write_ledger(data_home, [_row("f1", "filed", cr="")])
        diff = store.pr_queue_dir() / "f1.diff"
        diff.write_text("--- a\n", encoding="utf-8")
        result = la.purge("f1", remove_artifacts=False)
        assert result["ok"] is True and result["removed"] == []
        assert diff.is_file()

    def test_a_symlinked_artifact_is_not_followed_out_of_the_directory(
        self, data_home: Path
    ) -> None:
        """A symlink planted in the data directory must not redirect an unlink at a
        file outside it."""
        outside = data_home.parent / "outside.txt"
        outside.write_text("keep me", encoding="utf-8")
        link = store.pr_queue_dir() / "f1.diff"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            import pytest

            pytest.skip("symlink creation not permitted on this host (Windows without dev mode)")

        _write_ledger(data_home, [_row("f1", "filed", cr="")])
        result = la.purge("f1")
        assert result["ok"] is True
        assert outside.is_file()
        assert result["removed"] == []

    def test_an_unresolvable_artifact_directory_is_skipped(self, data_home: Path) -> None:
        """The ledger event has already been written by this point, so a directory that
        cannot be resolved must cost that one directory, not the whole purge."""
        _write_ledger(data_home, [_row("f1", "filed", cr="")])
        (store.profiles_dir() / "f1.json").write_text("{}", encoding="utf-8")

        real_resolve = Path.resolve

        def _flaky(self: Path, *args: Any, **kwargs: Any) -> Path:
            if self.name == "pr_queue":
                raise OSError("stale handle")
            return real_resolve(self, *args, **kwargs)

        with mock.patch.object(Path, "resolve", _flaky):
            result = la.purge("f1")
        assert result["ok"] is True
        assert result["removed"] == ["profiles/f1.json"]

    def test_a_directory_named_like_an_artifact_is_not_removed(self, data_home: Path) -> None:
        """The glob can match a directory; unlink on one raises, so it is filtered."""
        _write_ledger(data_home, [_row("f1", "filed", cr="")])
        stray = store.profiles_dir() / "f1.d"
        stray.mkdir()
        result = la.purge("f1")
        assert result["ok"] is True and result["removed"] == []
        assert stray.is_dir()

    def test_candidate_artifacts_are_deliberately_left_alone(self, data_home: Path) -> None:
        """``results/candidates/*`` is named after the cand_id, which embeds the
        target rather than the fingerprint. Matching it means guessing from a slug, and
        two findings in one function share that slug — deleting another finding's
        evidence is worse than a stale file."""
        _write_ledger(data_home, [_row("f1", "filed", cr="")])
        cand = store.results_dir() / "candidates"
        cand.mkdir(parents=True, exist_ok=True)
        artifact = cand / "c1_wide_x_py_f_abc.json"
        artifact.write_text("{}", encoding="utf-8")
        la.purge("f1")
        assert artifact.is_file()


class TestPurgeDead:
    def test_sweeps_only_the_dead_records(self, data_home: Path) -> None:
        _write_ledger(
            data_home,
            [
                _row("dead1", "filed", cr=""),
                _row("dead2", "filed", cr="not-a-url"),
                _row("alive", "filed", cr=REAL_PR),
                _row("queued", "filed", cr="QUEUED:queued"),
                _row("open", "seen"),
            ],
        )
        result = la.purge_dead()
        assert result["ok"] is True
        assert sorted(result["purged"]) == ["dead1", "dead2"]
        assert result["count"] == 2

        latest = {r["fp"]: r["status"] for r in la.latest_records()}
        assert latest == {
            "dead1": "purged",
            "dead2": "purged",
            "alive": "filed",
            "queued": "filed",
            "open": "seen",
        }

    def test_keeps_artifacts_by_default(self, data_home: Path) -> None:
        """A dead record is one whose pull request was never created, which makes the
        queued diff the only surviving copy of that change. A bulk sweep is the wrong
        place to discard it."""
        _write_ledger(data_home, [_row("dead1", "filed", cr="")])
        diff = store.pr_queue_dir() / "dead1.diff"
        diff.write_text("--- a\n", encoding="utf-8")
        assert la.purge_dead()["count"] == 1
        assert diff.is_file()

    def test_can_be_opted_into_removing_artifacts(self, data_home: Path) -> None:
        _write_ledger(data_home, [_row("dead1", "filed", cr="")])
        diff = store.pr_queue_dir() / "dead1.diff"
        diff.write_text("--- a\n", encoding="utf-8")
        assert la.purge_dead(remove_artifacts=True)["count"] == 1
        assert not diff.exists()

    def test_is_idempotent(self, data_home: Path) -> None:
        """A second sweep must find nothing: the purged event supersedes ``filed``, so
        the record is no longer dead."""
        _write_ledger(data_home, [_row("dead1", "filed", cr="")])
        assert la.purge_dead()["count"] == 1
        assert la.purge_dead() == {"ok": True, "purged": [], "count": 0}

    def test_empty_and_missing_ledgers_sweep_cleanly(self, data_home: Path) -> None:
        assert la.purge_dead() == {"ok": True, "purged": [], "count": 0}
        _write_ledger(data_home, [])
        assert la.purge_dead() == {"ok": True, "purged": [], "count": 0}

    def test_skips_a_record_whose_fingerprint_is_not_addressable(self, data_home: Path) -> None:
        """A fingerprint predating the shape rule cannot be turned into a path, so it
        is skipped rather than allowed to abort the whole sweep."""
        _write_ledger(
            data_home,
            [_row("bad/fp", "filed", cr=""), _row("dead1", "filed", cr="")],
        )
        result = la.purge_dead()
        assert result["purged"] == ["dead1"]


class TestLedgerResilience:
    def test_a_torn_tail_line_never_hides_earlier_entries(self, data_home: Path) -> None:
        """A run can be killed mid-append. One bad line must cost that one event, not
        the history before it — matching how the findings endpoint reads."""
        path = data_home / "ledger.jsonl"
        path.write_text(
            json.dumps(_row("f1", "seen"))
            + "\n"
            + json.dumps(_row("f1", "failed_gate"))
            + "\n"
            + '{"fp": "f1", "status": "fil',
            encoding="utf-8",
        )
        records = {r["fp"]: r["status"] for r in la.latest_records()}
        assert records == {"f1": "failed_gate"}
        assert la.forget("f1")["ok"] is True

    def test_a_corrupt_line_in_the_middle_is_skipped(self, data_home: Path) -> None:
        path = data_home / "ledger.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(_row("f1", "seen")),
                    "}}} not json {{{",
                    json.dumps(_row("f2", "filed", cr=REAL_PR)),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        records = {r["fp"]: r["status"] for r in la.latest_records()}
        assert records == {"f1": "seen", "f2": "filed"}

    def test_non_dict_and_fp_less_rows_are_ignored(self, data_home: Path) -> None:
        path = data_home / "ledger.jsonl"
        path.write_text(
            "\n".join(
                [
                    "[1, 2, 3]",
                    '"a string"',
                    "null",
                    '{"status": "filed"}',
                    json.dumps(_row("f1", "seen")),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        assert [r["fp"] for r in la.latest_records()] == ["f1"]

    def test_blank_lines_are_tolerated(self, data_home: Path) -> None:
        path = data_home / "ledger.jsonl"
        path.write_text(
            f"\n{json.dumps(_row('f1', 'seen'))}\n\n\n{json.dumps(_row('f1', 'filed', cr=''))}\n",
            encoding="utf-8",
        )
        assert la.purge_dead()["purged"] == ["f1"]

    def test_latest_wins_in_file_order_not_timestamp_order(self, data_home: Path) -> None:
        """Append order is the ledger's own notion of latest and every reader uses it,
        so a row with a skewed or missing ``ts`` must resolve the same way here."""
        _write_ledger(
            data_home,
            [_row("f1", "filed", cr=REAL_PR, ts=9999.0), _row("f1", "failed_gate", ts=1.0)],
        )
        assert {r["fp"]: r["status"] for r in la.latest_records()} == {"f1": "failed_gate"}

    def test_latest_records_normalizes_the_reference_key(self, data_home: Path) -> None:
        _write_ledger(data_home, [_row("f1", "filed", cr=REAL_PR)])
        assert la.latest_records()[0]["pr"] == REAL_PR

    def test_concurrent_forgets_append_one_event_each(self, data_home: Path) -> None:
        """The lock serializes read → decide → append, so two callers cannot both
        conclude "no prior event" and neither can lose the other's line."""
        _write_ledger(data_home, [_row(f"f{i}", "failed_gate") for i in range(8)])
        results: list[dict[str, Any]] = []
        threads = [
            threading.Thread(target=lambda i=i: results.append(la.forget(f"f{i}")))  # type: ignore[misc]
            for i in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert all(r["ok"] for r in results)
        rows = _events(data_home)
        assert len(rows) == 16  # every append landed, none interleaved into a torn line
        assert sum(1 for r in rows if r["status"] == "purged") == 8

    def test_the_ledger_is_created_when_absent(self, data_home: Path) -> None:
        """A forget on a fresh install must refuse, not create a stray file."""
        assert not store.ledger_path().exists()
        la.forget("f1")
        assert not store.ledger_path().exists()


class TestRealData:
    """Parse the machine's live ledger, if there is one, rather than only fixtures."""

    @staticmethod
    def _live() -> Path:
        return Path.home() / ".kiro" / "crew" / "apps" / "auto-improvement" / "data"

    def test_the_live_ledger_parses_and_classifies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = self._live() / "ledger.jsonl"
        if not live.is_file():
            pytest.skip("no live ledger on this machine")
        # Copy rather than point at the real data: nothing here may write to it.
        copy_root = tmp_path / "data"
        copy_root.mkdir(parents=True, exist_ok=True)
        (copy_root / "ledger.jsonl").write_text(live.read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.setattr(store, "data_dir", lambda: copy_root)
        monkeypatch.setattr(store, "workspace_dir", lambda: copy_root)

        records = la.latest_records()
        assert records, "the live ledger should hold at least one event"
        for record in records:
            assert record["fp"]
            assert isinstance(record["pr"], str)
            assert isinstance(la.is_dead_record(record), bool)
        # The live ledger's one filed record carries a real pull request, so the sweep
        # must leave it alone.
        filed = [r for r in records if r.get("status") == "filed"]
        for record in filed:
            if la.is_real_pr_reference(la.pr_reference(record)):
                assert la.is_dead_record(record) is False


class TestManualDraftRowSurvivesReload:
    """``routes.ledger_admin_record`` must write a row the spine ledger can actually
    load. Raised by review of this branch: it wrote the reference under ``pr``, and
    ``LedgerEntry`` has ``cr`` plus REQUIRED ``kind``/``target``, so the row raised
    ``TypeError`` inside ``_load()``'s torn-line handler and was silently discarded.

    Consequence if it regresses: the ``filed`` marker never enters the dedup index, so
    after the retry cooldown the loop re-discovers the locus and drafts a SECOND pull
    request for a change that is already filed. Same failure mode ``_purged_event``
    documents — which is why this asserts on a real reload, not on the dict shape.
    """

    def test_filed_marker_enters_the_dedup_index(self, data_home: Path) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import routes
        from kiro_crew.apps.builtins.auto_improvement.spine import ledger as spine_ledger

        # A prior soft-terminal row, as after an automatic draft failed (no gh/network).
        _write_ledger(data_home, [_row("f9", "error")])
        routes.ledger_admin_record("f9", "https://github.com/o/r/pull/7")

        reloaded = spine_ledger.Ledger(store.ledger_path())
        assert "f9" in reloaded._seen, "the row must not be dropped as a torn line"
        entry = reloaded._seen["f9"]
        assert entry.status == "filed"
        assert entry.cr == "https://github.com/o/r/pull/7"
        assert reloaded.known("f9") is True, "a filed change must stay deduped"

    def test_row_is_loadable_even_with_no_prior_row(self, data_home: Path) -> None:
        """``kind``/``target`` are required, so they must be present unconditionally —
        not only on the path where a prior row supplied them."""
        from kiro_crew.apps.builtins.auto_improvement.backend import routes
        from kiro_crew.apps.builtins.auto_improvement.spine import ledger as spine_ledger

        store.ledger_path().parent.mkdir(parents=True, exist_ok=True)
        routes.ledger_admin_record("f10", "https://github.com/o/r/pull/8")

        reloaded = spine_ledger.Ledger(store.ledger_path())
        assert "f10" in reloaded._seen
        assert reloaded._seen["f10"].cr.endswith("/8")
