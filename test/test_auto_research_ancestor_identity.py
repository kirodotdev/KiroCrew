from __future__ import annotations

import json
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.apps.builtins.auto_research import handlers as h

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX permits live directory renames")


class _Request(dict):
    def __init__(self, campaign_id: str, app: dict | None = None) -> None:
        super().__init__(user="test-user")
        self.match_info = {"id": campaign_id}
        self.app = app or {}


def _body(response) -> dict:
    return json.loads(response.text or "{}")


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "RESEARCH_DIR", tmp_path / "owner" / "research")
    monkeypatch.setattr(h, "DB_PATH", tmp_path / "campaigns.db")
    campaign_id = h.create_campaign(
        {
            "question": "Research a sufficiently detailed ancestor ownership question",
            "sources": ["web"],
        }
    )["id"]
    directory = h._campaign_dir(campaign_id)
    (directory / "FINDINGS.md").write_text("owned report", encoding="utf-8")
    (directory / "grill_tree.json").write_text('[{"text":"owned"}]', encoding="utf-8")
    (directory / "questions.json").write_text('{"question":"owned"}', encoding="utf-8")
    (directory / h._WORKFLOW_RUN_FILE).write_text(
        '{"run_id":"owned-run","cycle_offset":1}', encoding="utf-8"
    )
    (directory / h._WORKER_DONE_FILENAME).write_text('{"reason":"owned"}', encoding="utf-8")
    (directory / "findings" / "cycle_001.json").write_text(
        '{"cycle":1,"new_findings_count":1}', encoding="utf-8"
    )
    return campaign_id, directory


def _replace_root_preserving_campaign(identity: h._CampaignIdentity) -> None:
    parked = identity.root.with_name(f"{identity.root.name}-parked")
    identity.root.rename(parked)
    identity.root.mkdir()
    (parked / identity.campaign_id).rename(identity.directory)


def _swap_after_capture(monkeypatch, campaign_id: str) -> None:
    real_identity = h._campaign_identity
    swapped = False

    def _capture(requested: str):
        nonlocal swapped
        identity = real_identity(requested)
        if requested == campaign_id and identity is not None and not swapped:
            swapped = True
            _replace_root_preserving_campaign(identity)
        return identity

    monkeypatch.setattr(h, "_campaign_identity", _capture)


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["report", "artifact", "knowledge", "grill"])
async def test_handlers_fail_closed_after_ancestor_replacement(campaign, monkeypatch, consumer):
    campaign_id, _directory = campaign
    artifact_store = MagicMock()
    knowledge_store = MagicMock()
    knowledge_pipeline = SimpleNamespace(ingest_file=AsyncMock())
    monkeypatch.setattr(h, "_HAS_ARTIFACTS", True)
    monkeypatch.setattr(h, "ArtifactStore", lambda: artifact_store)
    _swap_after_capture(monkeypatch, campaign_id)

    if consumer == "report":
        response = await h._handle_report(_Request(campaign_id))
        assert response.status == 200
        assert _body(response) == {"report": ""}
    elif consumer == "artifact":
        response = await h._handle_to_artifact(_Request(campaign_id))
        assert response.status == 409
        assert _body(response)["code"] == "findings_refused"
        assert artifact_store.method_calls == []
    elif consumer == "knowledge":
        app = {
            "state": SimpleNamespace(knowledge_store=knowledge_store),
            "knowledge_pipeline": knowledge_pipeline,
        }
        response = await h._handle_to_knowledge(_Request(campaign_id, app))
        assert response.status == 409
        assert _body(response)["code"] == "findings_refused"
        assert knowledge_store.method_calls == []
        knowledge_pipeline.ingest_file.assert_not_awaited()
    else:
        response = await h._handle_grill_tree(_Request(campaign_id))
        assert response.status == 200
        assert _body(response) == {"tree": []}


@pytest.mark.parametrize(
    ("consumer", "expected"),
    [
        ("findings", []),
        ("cycles", []),
        ("question", None),
        ("workflow", None),
        ("worker_done", None),
    ],
)
def test_sibling_consumers_fail_closed_after_ancestor_replacement(
    campaign, monkeypatch, consumer, expected
):
    campaign_id, _directory = campaign
    _swap_after_capture(monkeypatch, campaign_id)
    calls = {
        "findings": h.get_findings,
        "cycles": h._list_cycle_files,
        "question": h._pending_question,
        "workflow": h._read_workflow_run_id,
        "worker_done": h._read_worker_done,
    }
    assert calls[consumer](campaign_id) == expected


@pytest.mark.parametrize("consumer", ["delete", "cleanup"])
def test_mutating_consumers_keep_owned_bytes_after_ancestor_replacement(
    campaign, monkeypatch, consumer
):
    campaign_id, directory = campaign
    _swap_after_capture(monkeypatch, campaign_id)
    marker = directory / h._WORKER_DONE_FILENAME

    if consumer == "delete":
        assert h.delete_campaign(campaign_id) == {
            "error": "cleanup incomplete",
            "residual": True,
        }
        assert (directory / "FINDINGS.md").read_text(encoding="utf-8") == "owned report"
        assert h.get_campaign(campaign_id) is not None
    else:
        h._clear_worker_done_marker(campaign_id)
        assert marker.read_text(encoding="utf-8") == '{"reason":"owned"}'


def test_MUTATION_dropping_direct_containment_redirects_report(campaign, monkeypatch):
    campaign_id, _directory = campaign
    identity = h._campaign_identity(campaign_id)
    assert identity is not None
    _replace_root_preserving_campaign(identity)

    @contextmanager
    def _unsafe_pin(self):
        campaign_fd = os.dup(self._campaign_fd)
        try:
            yield campaign_fd
        finally:
            os.close(campaign_fd)

    monkeypatch.setattr(h._CampaignIdentity, "pin", _unsafe_pin)
    try:
        assert h._read_report_for_identity(identity) == "owned report"
    finally:
        identity.close()


def test_MUTATION_dropping_containment_allows_redirected_cleanup(campaign, monkeypatch):
    campaign_id, directory = campaign
    _swap_after_capture(monkeypatch, campaign_id)

    @contextmanager
    def _unsafe_pin(self):
        campaign_fd = os.dup(self._campaign_fd)
        try:
            yield campaign_fd
        finally:
            os.close(campaign_fd)

    monkeypatch.setattr(h._CampaignIdentity, "pin", _unsafe_pin)
    h._clear_worker_done_marker(campaign_id)
    assert not (directory / h._WORKER_DONE_FILENAME).exists()


def test_identity_finalizer_offloads_descriptor_closure(monkeypatch, tmp_path):
    """A leaked identity must not synchronously close descriptors on an event loop."""
    from kiro_crew.apps.builtins.auto_research import handlers as h

    scheduled: list[tuple[object, tuple[int, ...]]] = []

    class _Loop:
        def run_in_executor(self, executor, callback, *fds):
            scheduled.append((callback, fds))

    monkeypatch.setattr(h.asyncio, "get_running_loop", lambda: _Loop())
    identity = h._CampaignIdentity(
        "a1b2c3d4",
        tmp_path,
        tmp_path / "a1b2c3d4",
        "research-a1b2c3d4",
        1,
        2,
        71,
        72,
    )

    identity.__del__()

    assert scheduled == [(h._close_fds, (72, 71))]
    assert identity._campaign_fd == identity._root_fd == -1


@pytest.mark.skipif(
    os.name != "posix",
    reason="the replacement race requires POSIX directory rename semantics",
)
def test_campaign_write_revalidates_identity_after_leaf_write(tmp_path, monkeypatch):
    """A write through a stale descriptor must fail rather than report success."""
    from kiro_crew.apps.builtins.auto_research import handlers as h

    root = tmp_path / "research"
    requested = root / "a1b2c3d4"
    replacement = root / "b1c2d3e4"
    requested.mkdir(parents=True)
    replacement.mkdir()
    monkeypatch.setattr(h, "RESEARCH_DIR", root)
    identity = h._campaign_identity("a1b2c3d4")
    assert identity is not None
    original_ftruncate = h.os.ftruncate
    swapped = False

    def _swap_after_open(fd: int, size: int) -> None:
        nonlocal swapped
        original_ftruncate(fd, size)
        if not swapped:
            swapped = True
            requested.rename(root / "parked-requested")
            replacement.rename(requested)

    monkeypatch.setattr(h.os, "ftruncate", _swap_after_open)
    try:
        with pytest.raises(PermissionError, match="campaign name"):
            h._write_campaign_text(identity, "status.json", "owned")
    finally:
        identity.close()

    assert not (requested / "status.json").exists()


@pytest.mark.skipif(
    os.name != "posix",
    reason="the replacement race requires POSIX directory rename semantics",
)
def test_MUTATION_skipping_write_exit_revalidation_loses_the_named_write(tmp_path, monkeypatch):
    """Without exit revalidation, a replacement makes a successful write disappear."""
    from contextlib import contextmanager

    from kiro_crew.apps.builtins.auto_research import handlers as h

    root = tmp_path / "research"
    requested = root / "a1b2c3d4"
    replacement = root / "b1c2d3e4"
    requested.mkdir(parents=True)
    replacement.mkdir()
    monkeypatch.setattr(h, "RESEARCH_DIR", root)
    identity = h._campaign_identity("a1b2c3d4")
    assert identity is not None
    original_ftruncate = h.os.ftruncate
    swapped = False

    def _swap_after_open(fd: int, size: int) -> None:
        nonlocal swapped
        original_ftruncate(fd, size)
        if not swapped:
            swapped = True
            requested.rename(root / "parked-requested")
            replacement.rename(requested)

    @contextmanager
    def _unsafe_pin():
        campaign_fd = os.dup(identity._campaign_fd)
        try:
            yield campaign_fd
        finally:
            os.close(campaign_fd)

    monkeypatch.setattr(h.os, "ftruncate", _swap_after_open)
    monkeypatch.setattr(identity, "pin", _unsafe_pin)
    try:
        h._write_campaign_text(identity, "status.json", "lost")
    finally:
        identity.close()

    assert (root / "parked-requested" / "status.json").read_text(encoding="utf-8") == "lost"
    assert not (requested / "status.json").exists()


@pytest.mark.skipif(
    os.name != "posix",
    reason="the root-link swap requires POSIX symlink semantics",
)
def test_captured_identity_refuses_research_root_link_swap(tmp_path, monkeypatch):
    """A captured campaign must not outlive a changed named research root."""
    from kiro_crew.apps.builtins.auto_research import handlers as h

    original_root = tmp_path / "original-research"
    replacement_root = tmp_path / "replacement-research"
    original_root.mkdir()
    replacement_root.mkdir()
    root_link = tmp_path / "research"
    root_link.symlink_to(original_root, target_is_directory=True)
    monkeypatch.setattr(h, "RESEARCH_DIR", root_link)
    monkeypatch.setattr(h, "DB_PATH", tmp_path / "campaigns.db")
    campaign_id = h.create_campaign(
        {
            "question": "Research root link ownership must remain stable after capture",
            "sources": ["web"],
        }
    )["id"]
    identity = h._campaign_identity(campaign_id)
    assert identity is not None

    root_link.unlink()
    root_link.symlink_to(replacement_root, target_is_directory=True)
    try:
        with pytest.raises(PermissionError, match="research root identity changed"):
            with identity.pin():
                pass
    finally:
        identity.close()


@pytest.mark.skipif(
    os.name != "posix",
    reason="the root replacement requires POSIX rename semantics",
)
def test_fresh_identity_accepts_campaign_under_recreated_root(campaign):
    """A new root does not inherit a prior root's campaign identity record."""
    campaign_id, directory = campaign
    root = directory.parent
    parked = root.with_name("parked-research")
    root.rename(parked)
    root.mkdir()
    (root / campaign_id).mkdir()

    fresh = h._campaign_identity(campaign_id)
    assert fresh is not None
    try:
        with fresh.pin():
            pass
    finally:
        fresh.close()


@pytest.mark.skipif(
    os.name != "posix",
    reason="the root-link swap requires POSIX symlink semantics",
)
def test_MUTATION_skipping_live_root_binding_reads_detached_campaign(tmp_path, monkeypatch):
    """Without the root check, a changed link still serves the detached campaign."""
    from kiro_crew.apps.builtins.auto_research import handlers as h

    original_root = tmp_path / "original-research"
    replacement_root = tmp_path / "replacement-research"
    original_root.mkdir()
    replacement_root.mkdir()
    root_link = tmp_path / "research"
    root_link.symlink_to(original_root, target_is_directory=True)
    monkeypatch.setattr(h, "RESEARCH_DIR", root_link)
    monkeypatch.setattr(h, "DB_PATH", tmp_path / "campaigns.db")
    campaign_id = h.create_campaign(
        {
            "question": "Detached campaign reads must fail after a root link swap",
            "sources": ["web"],
        }
    )["id"]
    directory = original_root / campaign_id
    (directory / "FINDINGS.md").write_text("detached report", encoding="utf-8")
    identity = h._campaign_identity(campaign_id)
    assert identity is not None

    root_link.unlink()
    root_link.symlink_to(replacement_root, target_is_directory=True)
    monkeypatch.setattr(h._CampaignIdentity, "_validate_live_root", lambda _self: None)
    try:
        assert h._read_report_for_identity(identity) == "detached report"
    finally:
        identity.close()
