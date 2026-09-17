"""Tests for the Research Lab I/O ownership and identity-registry repairs.

Each test pins one exact defect in ``auto_research/handlers.py`` and fails on
the pre-repair code:

* ``_read_campaign_file_bytes`` handed a descriptor to ``os.fdopen`` but only
  recorded the transfer after ``read()`` returned, so a read failure closed the
  descriptor twice (once by the file object, once by ``finally``).
* ``_capture_run_finding_snapshot`` answered a proven-empty ``"[]"`` when the
  campaign directory could not be pinned at all, letting historical evidence
  count as current-generation evidence on the next cycle-cap check.
* ``delete_campaign`` returned ``campaign not found`` after removing the tree
  but before retiring the inode pin, so the id stayed refused in-process.
* The knowledge export carried a dead ``ingest_file`` fallback; the sanitized
  text already read through the pinned identity is the only ingestion input.
* Campaign pinning has exactly one seam, ``_CampaignIdentity.pin``.
"""

from __future__ import annotations

import errno
import io
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.auto_research import handlers as h

BASE = "/api/apps/auto-research"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path):
    with (
        mock.patch.object(h, "DB_PATH", tmp_path / "t.db"),
        mock.patch.object(h, "RESEARCH_DIR", tmp_path / "r"),
    ):
        yield tmp_path


@pytest.fixture(autouse=True)
def _no_autonudge(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(h, "_autonudge_instance", lambda: None)


def _campaign() -> str:
    return h.create_campaign(
        {"question": "How do teams handle API rate limiting today?", "sources": ["web"]}
    )["id"]


def _registry_keys_for(cid: str) -> list[tuple]:
    with h._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
        return [key for key in h._CAMPAIGN_DIRECTORY_IDENTITIES if key[3] == cid]


class TestCampaignFileReadDescriptorOwnership:
    @pytest.mark.skipif(os.name != "posix", reason="descriptor-relative reads are POSIX-only")
    def test_read_failure_closes_the_file_descriptor_exactly_once(self, monkeypatch):
        cid = _campaign()
        report = h._campaign_dir(cid) / "FINDINGS.md"
        report.write_text("owned", encoding="utf-8")
        report_inode = (report.stat().st_dev, report.stat().st_ino)
        identity = h._campaign_identity(cid)
        assert identity is not None

        opened: list[int] = []
        closed_after_transfer: list[int] = []
        real_fdopen = os.fdopen
        real_close = os.close

        class _ReadFails(io.FileIO):
            def read(self, *args: Any, **kwargs: Any) -> bytes:
                raise OSError(errno.EIO, "read failed")

        def _fdopen(fd: int, mode: str = "r", *args: Any, **kwargs: Any):
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != report_inode:
                return real_fdopen(fd, mode, *args, **kwargs)
            opened.append(fd)
            return _ReadFails(fd, mode)

        def _close(fd: int) -> None:
            # Descriptor numbers are reused during pin validation before the
            # file is opened; only closes after the transfer can double-close it.
            if opened:
                closed_after_transfer.append(fd)
            real_close(fd)

        monkeypatch.setattr(os, "fdopen", _fdopen)
        monkeypatch.setattr(os, "close", _close)
        try:
            result = h._read_campaign_file_bytes(identity, ("FINDINGS.md",), max_bytes=1024)
        finally:
            monkeypatch.setattr(os, "fdopen", real_fdopen)
            monkeypatch.setattr(os, "close", real_close)
            identity.close()

        assert result is None
        assert len(opened) == 1
        file_fd = opened[0]
        # The file object closed it once; ``finally`` must not try again, because
        # a second close can hit a descriptor number another thread reused.
        assert file_fd not in closed_after_transfer
        with pytest.raises(OSError) as leaked:
            os.fstat(file_fd)
        assert leaked.value.errno == errno.EBADF

    @pytest.mark.skipif(os.name != "posix", reason="descriptor-relative reads are POSIX-only")
    def test_successful_read_still_returns_the_bytes(self):
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_bytes(b"owned report")
        identity = h._campaign_identity(cid)
        assert identity is not None
        try:
            assert (
                h._read_campaign_file_bytes(identity, ("FINDINGS.md",), max_bytes=1024)
                == b"owned report"
            )
        finally:
            identity.close()


class TestRunFindingSnapshotUnknownOnUnavailableDirectory:
    def _finding(self, cid: str, cycle: int) -> Path:
        path = h._campaign_dir(cid) / "findings" / f"cycle_{cycle:03d}.json"
        path.write_text(f'{{"summary":"historical {cycle}"}}', encoding="utf-8")
        return path

    def test_unpinnable_campaign_directory_is_unknown_not_empty(self):
        cid = _campaign()
        self._finding(cid, 1)
        directory = h._campaign_dir(cid)
        parked = directory.with_name(f"{cid}-parked")
        directory.rename(parked)
        try:
            assert h._safe_campaign_dir(cid) is None
            assert h._capture_run_finding_snapshot(cid) is None
        finally:
            parked.rename(directory)

    def test_start_over_a_transient_pin_failure_refuses_cap_completion_later(self, monkeypatch):
        """Historical findings must never satisfy a cap after an UNKNOWN Start."""
        cid = _campaign()
        files = [self._finding(cid, 1), self._finding(cid, 2)]
        real_pin = h._CampaignIdentity.pin
        failed: list[str] = []

        def _pin_once_exhausted(self, *, revalidate_on_exit=True):
            if not failed:
                failed.append("EMFILE")
                raise OSError(errno.EMFILE, "too many open files")
            return real_pin(self, revalidate_on_exit=revalidate_on_exit)

        monkeypatch.setattr(h._CampaignIdentity, "pin", _pin_once_exhausted)
        result = h.update_campaign_status(cid, h.CampaignStatus.RUNNING)
        monkeypatch.setattr(h._CampaignIdentity, "pin", real_pin)

        assert failed == ["EMFILE"], "the snapshot capture is the first pin of the transition"
        assert result == {"id": cid, "status": h.CampaignStatus.RUNNING}
        campaign = h.get_campaign(cid)
        assert campaign is not None
        assert campaign["run_finding_snapshot"] is None
        assert h._run_finding_snapshot(cid) is None
        assert not h._cycle_cap_generation_complete(cid, files, 2)

    def test_reachable_empty_directory_is_still_a_proven_empty_snapshot(self):
        """Control: only an unreachable directory is UNKNOWN."""
        cid = _campaign()
        shutil.rmtree(h._campaign_dir(cid) / "findings")
        assert h._capture_run_finding_snapshot(cid) == "[]"

    def test_reachable_directory_snapshot_fences_its_historical_findings(self):
        cid = _campaign()
        files = [self._finding(cid, 1), self._finding(cid, 2)]
        h.update_campaign_status(cid, h.CampaignStatus.RUNNING)
        historical = h._run_finding_snapshot(cid)
        assert historical is not None
        assert sum(historical.values()) == 2
        assert not h._cycle_cap_generation_complete(cid, files, 2)


class TestDeleteRetiresIdentityPinWithoutARow:
    def test_vanished_row_delete_removes_the_tree_and_the_pin(self):
        cid = _campaign()
        directory = h._campaign_dir(cid)
        (directory / "FINDINGS.md").write_text("owned", encoding="utf-8")
        assert _registry_keys_for(cid), "the campaign directory must be pinned first"
        db = h._get_db()
        try:
            db.execute("DELETE FROM campaigns WHERE id = ?", (cid,))
            db.commit()
        finally:
            db.close()

        assert h.delete_campaign(cid) == {"error": "campaign not found"}

        assert not directory.exists()
        assert _registry_keys_for(cid) == []
        # A later directory reusing this id gets a fresh inode; it must not be
        # refused by a pin that outlived the tree it protected.
        recreated = h._campaign_dir(cid)
        assert recreated == directory
        assert recreated.is_dir()

    def test_present_row_delete_still_retires_the_pin(self):
        cid = _campaign()
        directory = h._campaign_dir(cid)
        assert _registry_keys_for(cid)
        assert h.delete_campaign(cid) == {"id": cid, "deleted": True, "residual": False}
        assert not directory.exists()
        assert _registry_keys_for(cid) == []

    def test_database_delete_commits_before_tree_removal(self, monkeypatch):
        cid = _campaign()
        directory = h._campaign_dir(cid)
        (directory / "owned.txt").write_text("owned", encoding="utf-8")
        real_remove = h._remove_campaign_tree
        rows_seen_during_removal: list[object] = []

        def _observe_committed_delete(identity):
            db = h._get_db()
            try:
                rows_seen_during_removal.append(
                    db.execute("SELECT 1 FROM campaigns WHERE id = ?", (cid,)).fetchone()
                )
            finally:
                db.close()
            return real_remove(identity)

        monkeypatch.setattr(h, "_remove_campaign_tree", _observe_committed_delete)

        assert h.delete_campaign(cid) == {"id": cid, "deleted": True, "residual": False}
        assert rows_seen_during_removal == [None]
        assert not directory.exists()

    def test_database_commit_failure_preserves_row_tree_and_identity(self, monkeypatch):
        cid = _campaign()
        directory = h._campaign_dir(cid)
        marker = directory / "owned.txt"
        marker.write_text("owned", encoding="utf-8")
        original_registry = _registry_keys_for(cid)
        assert original_registry
        real_get_db = h._get_db
        connection = real_get_db()

        class _CommitFails:
            def execute(self, *args, **kwargs):
                return connection.execute(*args, **kwargs)

            def commit(self):
                raise h.sqlite3.OperationalError("commit refused")

            def rollback(self):
                return connection.rollback()

            def close(self):
                return connection.close()

        monkeypatch.setattr(h, "_get_db", lambda: _CommitFails())
        try:
            with pytest.raises(h.sqlite3.OperationalError, match="commit refused"):
                h.delete_campaign(cid)
        finally:
            monkeypatch.setattr(h, "_get_db", real_get_db)

        assert marker.read_text(encoding="utf-8") == "owned"
        assert h.get_campaign(cid) is not None
        assert _registry_keys_for(cid) == original_registry

    def test_incomplete_cleanup_keeps_the_pin_and_the_row(self):
        """Opposite path: a refused removal retains both authority and retry."""
        cid = _campaign()
        db = h._get_db()
        try:
            before = dict(db.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone())
        finally:
            db.close()
        with mock.patch.object(h, "_remove_campaign_tree", return_value=["in use"]):
            assert h.delete_campaign(cid) == {"error": "cleanup incomplete", "residual": True}
        db = h._get_db()
        try:
            after = dict(db.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone())
        finally:
            db.close()
        assert after == before
        assert h.get_campaign(cid) is not None
        assert _registry_keys_for(cid)


def _app(**keys: Any) -> web.Application:
    app = web.Application()
    for key, value in keys.items():
        app[key] = value
    return app


def _request(cid: str, app: web.Application) -> web.Request:
    req = make_mocked_request("POST", f"{BASE}/campaigns/{cid}/to-knowledge", app=app)
    req["user"] = "test-user"
    req.match_info.update({"id": cid})  # type: ignore[attr-defined]
    return req


async def _drain(app: web.Application) -> None:
    import asyncio

    tasks = list(app.get("_bg_tasks") or ())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class TestKnowledgeIngestUsesSanitizedTextOnly:
    @pytest.mark.asyncio
    async def test_ingest_text_receives_the_redacted_report_and_ingest_file_is_never_used(
        self,
    ):
        cid = _campaign()
        raw = "# Summary\nFound aws_secret=AKIAIOSFODNN7EXAMPLE in the config."
        (h._campaign_dir(cid) / "FINDINGS.md").write_text(raw, encoding="utf-8")
        store = MagicMock()
        store.get_source_by_uri.return_value = None
        store.add_source.return_value = 11
        pipeline = SimpleNamespace(ingest_text=AsyncMock(), ingest_file=AsyncMock())
        app = _app(state=SimpleNamespace(knowledge_store=store), knowledge_pipeline=pipeline)

        resp = await h._handle_to_knowledge(_request(cid, app))
        assert resp.status == 201
        await _drain(app)

        pipeline.ingest_file.assert_not_awaited()
        pipeline.ingest_text.assert_awaited_once()
        text, name = pipeline.ingest_text.await_args.args
        assert pipeline.ingest_text.await_args.kwargs == {"source_id": 11}
        sanitized = (h._campaign_dir(cid) / "findings_for_knowledge.md").read_text(encoding="utf-8")
        assert text == sanitized
        assert "AKIAIOSFODNN7EXAMPLE" not in text
        assert name == store.add_source.call_args.kwargs["name"]
        assert name.startswith("Research: ")
        statuses = [c.args[0] for c in store.db.execute.call_args_list]
        assert any("'synced'" in s for s in statuses)

    @pytest.mark.asyncio
    async def test_a_pipeline_without_ingest_text_never_falls_back_to_a_by_name_read(self):
        """Opposite path: no ``ingest_file(uri)`` re-read of the export by name."""
        cid = _campaign()
        (h._campaign_dir(cid) / "FINDINGS.md").write_text("findings body", encoding="utf-8")
        store = MagicMock()
        store.get_source_by_uri.return_value = None
        store.add_source.return_value = 12
        pipeline = SimpleNamespace(ingest_file=AsyncMock())
        app = _app(state=SimpleNamespace(knowledge_store=store), knowledge_pipeline=pipeline)

        resp = await h._handle_to_knowledge(_request(cid, app))
        assert resp.status == 201
        await _drain(app)

        pipeline.ingest_file.assert_not_awaited()
        statuses = [c.args[0] for c in store.db.execute.call_args_list]
        assert any("'error'" in s for s in statuses)
        assert not any("'synced'" in s for s in statuses)


class TestCampaignPinSeam:
    def test_pinning_has_no_module_level_wrapper(self):
        assert not hasattr(h, "_pin_campaign")
        assert callable(h._CampaignIdentity.pin)

    def test_every_reader_refuses_when_the_identity_pin_refuses(self, monkeypatch):
        from contextlib import contextmanager

        cid = _campaign()
        directory = h._campaign_dir(cid)
        (directory / "FINDINGS.md").write_text("owned", encoding="utf-8")
        finding = directory / "findings" / "cycle_001.json"
        finding.write_text('{"summary":"owned"}', encoding="utf-8")
        identity = h._campaign_identity(cid)
        assert identity is not None

        @contextmanager
        def _refuse(_identity, *, revalidate_on_exit=True):
            del revalidate_on_exit
            raise PermissionError("campaign directory changed")
            yield  # pragma: no cover

        monkeypatch.setattr(h._CampaignIdentity, "pin", _refuse)
        try:
            assert h._read_campaign_file_bytes(identity, ("FINDINGS.md",), max_bytes=64) is None
            assert h._recent_cycle_findings(identity) == []
            assert h._read_finding_bytes(finding) is None
            assert h._remove_campaign_tree(identity) == ["campaign directory changed"]
        finally:
            identity.close()
        assert finding.exists()


class TestFindingSnapshotIdentityBinding:
    def _finding(self, directory: Path, cycle: int, **payload: Any) -> Path:
        path = directory / "findings" / f"cycle_{cycle:03d}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _replacement_swap(self, cid: str, kind: str, **payload: Any):
        root = h.research_dir().resolve()
        original = root / cid
        if kind == "root":
            replacement_root = root.with_name(f"{root.name}-replacement")
            replacement = replacement_root / cid
            parked = root.with_name(f"{root.name}-captured")

            def _swap() -> None:
                root.rename(parked)
                replacement_root.rename(root)

        else:
            replacement = root / "b1c2d3e4"
            parked = root / "captured-campaign"

            def _swap() -> None:
                original.rename(parked)
                replacement.rename(original)

        self._finding(replacement, 1, **payload)
        return _swap

    def _swap_before_second_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cid: str,
        swap,
    ) -> list[int]:
        real_identity = h._campaign_identity
        calls: list[int] = []

        def _identity(requested: str):
            if requested == cid:
                calls.append(len(calls) + 1)
                if len(calls) == 2:
                    swap()
            return real_identity(requested)

        monkeypatch.setattr(h, "_campaign_identity", _identity)
        return calls

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permits live directory renames")
    @pytest.mark.parametrize("swap_kind", ["root", "campaign"])
    def test_run_snapshot_never_rebinds_after_admission(self, monkeypatch, swap_kind):
        cid = _campaign()
        original = self._finding(h._campaign_dir(cid), 1, summary="captured")
        expected = h._finding_content_identity(original.read_bytes())
        swap = self._replacement_swap(cid, swap_kind, summary="replacement")
        calls = self._swap_before_second_identity(monkeypatch, cid, swap)

        assert h._capture_run_finding_snapshot(cid) == f'["{expected}"]'
        assert calls == [1]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permits live directory renames")
    @pytest.mark.parametrize("swap_kind", ["root", "campaign"])
    def test_cycle_verdict_refuses_rebound_finding_bytes(self, monkeypatch, swap_kind):
        cid = _campaign()
        self._finding(h._campaign_dir(cid), 1, summary="captured")
        h.update_campaign_status(cid, h.CampaignStatus.RUNNING)
        swap = self._replacement_swap(
            cid,
            swap_kind,
            summary="replacement",
            verification={"passed": True},
        )
        calls = self._swap_before_second_identity(monkeypatch, cid, swap)

        cycle_files = h._list_cycle_files(cid)
        verdict = h._stalled_campaign_verdict(cid, cycle_files)

        assert verdict is None
        assert not h._finding_snapshot_known(cycle_files)
        assert calls == [1, 2]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permits live directory renames")
    def test_dashboard_findings_never_rebind_to_a_replacement_root(self, monkeypatch):
        cid = _campaign()
        self._finding(h._campaign_dir(cid), 1, summary="captured")
        swap = self._replacement_swap(cid, "root", summary="replacement")
        calls = self._swap_before_second_identity(monkeypatch, cid, swap)

        assert h.get_findings(cid) == [{"summary": "captured"}]
        assert calls == [1]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permits live directory renames")
    def test_stagnation_never_rebinds_to_a_replacement_root(self, monkeypatch):
        cid = _campaign()
        directory = h._campaign_dir(cid)
        for cycle in range(1, 6):
            self._finding(directory, cycle, new_findings_count=1)
        root = h.research_dir().resolve()
        replacement_root = root.with_name(f"{root.name}-replacement")
        for cycle in range(1, 6):
            self._finding(replacement_root / cid, cycle, new_findings_count=0)
        parked = root.with_name(f"{root.name}-captured")

        def _swap() -> None:
            root.rename(parked)
            replacement_root.rename(root)

        calls = self._swap_before_second_identity(monkeypatch, cid, _swap)

        assert h.check_stagnation(cid) is False
        assert calls == [1]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permits live directory renames")
    @pytest.mark.parametrize("swap_kind", ["root", "campaign"])
    def test_empty_run_snapshot_revalidates_after_capture(self, monkeypatch, swap_kind):
        cid = _campaign()
        swap = self._replacement_swap(cid, swap_kind, summary="replacement")
        real_init = h._FindingSnapshot.__init__
        swapped = False

        def _swap_before_result(snapshot, *args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                swap()
            real_init(snapshot, *args, **kwargs)

        monkeypatch.setattr(h._FindingSnapshot, "__init__", _swap_before_result)

        assert h._capture_run_finding_snapshot(cid) is None
        assert swapped

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permits live directory renames")
    @pytest.mark.parametrize("swap_kind", ["root", "campaign"])
    def test_empty_cycle_snapshot_result_revalidates_identity(self, monkeypatch, swap_kind):
        cid = _campaign()
        swap = self._replacement_swap(cid, swap_kind, summary="replacement")
        real_init = h._FindingSnapshot.__init__
        swapped = False

        def _swap_before_result(snapshot, *args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                swap()
            real_init(snapshot, *args, **kwargs)

        monkeypatch.setattr(h._FindingSnapshot, "__init__", _swap_before_result)

        snapshot = h._list_cycle_files(cid)
        assert snapshot == []
        assert not h._finding_snapshot_known(snapshot)
        assert swapped

    def test_empty_cycle_snapshot_without_replacement_is_known(self):
        cid = _campaign()
        snapshot = h._list_cycle_files(cid)

        assert snapshot == []
        assert h._finding_snapshot_known(snapshot)

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permits live directory renames")
    @pytest.mark.parametrize("swap_kind", ["root", "campaign"])
    def test_mid_snapshot_identity_swap_is_unknown(self, monkeypatch, swap_kind):
        cid = _campaign()
        self._finding(h._campaign_dir(cid), 1, summary="captured")
        swap = self._replacement_swap(cid, swap_kind, summary="replacement")
        real_read = h._read_campaign_file_bytes
        swapped = False

        def _swap_before_read(identity, relative_parts, **kwargs):
            nonlocal swapped
            if relative_parts[:1] == ("findings",) and not swapped:
                swapped = True
                swap()
            return real_read(identity, relative_parts, **kwargs)

        monkeypatch.setattr(h, "_read_campaign_file_bytes", _swap_before_read)

        assert h._capture_run_finding_snapshot(cid) is None
        assert swapped

    def _workspace_replacement(self, cid: str, kind: str):
        root = h.research_dir().resolve()
        original = root / cid
        if kind == "root":
            replacement_root = root.with_name(f"{root.name}-replacement-workspace")
            replacement = replacement_root / cid
            replacement.mkdir(parents=True)
            parked = root.with_name(f"{root.name}-captured-workspace")

            def _swap() -> None:
                root.rename(parked)
                replacement_root.rename(root)

        else:
            replacement = root / "c1d2e3f4"
            replacement.mkdir()
            parked = root / "captured-workspace"

            def _swap() -> None:
                original.rename(parked)
                replacement.rename(original)

        return replacement, _swap

    def _swap_when_first_identity_closes(self, monkeypatch, cid: str, swap):
        real_identity = h._campaign_identity
        armed = True

        def _identity(requested: str):
            nonlocal armed
            identity = real_identity(requested)
            if requested == cid and identity is not None and armed:
                armed = False
                real_close = identity.close

                def _close_and_swap() -> None:
                    real_close()
                    swap()

                identity.close = _close_and_swap
            return identity

        monkeypatch.setattr(h, "_campaign_identity", _identity)

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permits live directory renames")
    @pytest.mark.parametrize("swap_kind", ["root", "campaign"])
    def test_emergent_ingest_keeps_the_admitted_campaign_identity(self, monkeypatch, swap_kind):
        cid = _campaign()
        original = h._campaign_dir(cid)
        (original / h._EMERGENT_FILENAME).write_text(
            '[{"text":"captured question","priority":1.0}]', encoding="utf-8"
        )
        replacement, swap = self._workspace_replacement(cid, swap_kind)
        (replacement / h._EMERGENT_FILENAME).write_text(
            '[{"text":"replacement question","priority":1.0}]', encoding="utf-8"
        )
        self._swap_when_first_identity_closes(monkeypatch, cid, swap)

        admitted = h._ingest_emergent_questions(cid)

        assert [item["text"] for item in admitted] == ["captured question"]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permits live directory renames")
    @pytest.mark.parametrize("swap_kind", ["root", "campaign"])
    def test_emergent_activation_keeps_queue_and_brief_on_admitted_identity(
        self, monkeypatch, swap_kind
    ):
        cid = _campaign()
        original = h._campaign_dir(cid)
        original_queue = h._sq.new_queue()
        h._sq.enqueue(original_queue, [{"text": "captured question", "priority": 1.0}])
        h._sq.save_queue(original, original_queue)
        replacement, swap = self._workspace_replacement(cid, swap_kind)
        replacement_queue = h._sq.new_queue()
        h._sq.enqueue(
            replacement_queue,
            [{"text": "replacement question", "priority": 1.0}],
        )
        h._sq.save_queue(replacement, replacement_queue)
        self._swap_when_first_identity_closes(monkeypatch, cid, swap)

        activated = h._activate_emergent(cid)

        assert [item["text"] for item in activated] == ["captured question"]
        db = h._get_db()
        try:
            row = db.execute("SELECT sub_questions FROM campaigns WHERE id = ?", (cid,)).fetchone()
        finally:
            db.close()
        assert "captured question" in row["sub_questions"]
        assert "replacement question" not in row["sub_questions"]
