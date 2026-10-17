"""Route tests: duplicating a spec.

The copy is staged, written through the reservation's pinned stage directory,
fsynced and published without replacing anything; startup recovery finishes or
discards a duplicate a dead process left behind; and the duplicate route refuses
a busy, mixed, unreadable or name-colliding source.
"""

from __future__ import annotations

import asyncio
import os
import threading
import types
from pathlib import Path
from typing import cast

import pytest
from aiohttp import web

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _make_client,
    _never_touch_the_real_state,
    _redirect_state,
    _seed_spec,
    _spec_identity,
    _state_for,
)
from kiro_crew.testing.wait import async_wait_until, default_timeout


def _stage_identity(path: Path) -> dict[str, int]:
    """Serialize the inode identity captured with a duplicate reservation."""
    info = path.stat()
    return {"stage_dev": info.st_dev, "stage_ino": info.st_ino}


# ── stale-index writes (concurrent delete resurrects a spec) ─────────────────


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="atomic no-replace directory publication is unavailable",
)
def test_duplicate_publication_never_replaces_a_destination_created_concurrently(
    tmp_path, monkeypatch
):
    """The destination's creation and the publish syscall are one arbitration.

    An existence check followed by ordinary rename loses this race on POSIX:
    rename replaces an empty directory, including its identity and metadata.
    """
    stage = tmp_path / ".copy.stage"
    stage.mkdir()
    (stage / "requirements.md").write_text("# staged")
    target = tmp_path / "copy"
    target_identity: list[tuple[int, int]] = []
    real_rename = routes.rename_noreplace

    def _writer_wins_before_the_syscall(*args, **kwargs):
        target.mkdir(mode=0o700)
        stat = target.stat()
        target_identity.append((stat.st_dev, stat.st_ino))
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(routes, "rename_noreplace", _writer_wins_before_the_syscall)

    assert routes._publish_staged_copy(stage, target) == "conflict"
    after = target.stat()
    assert (after.st_dev, after.st_ino) == target_identity[0]
    assert stage.is_dir(), "the losing staged copy replaced the concurrent destination"


@pytest.mark.skipif(
    not routes._CAN_PIN_DIR,
    reason="descriptor-pinned duplicate writes are unavailable",
)
def test_duplicate_doc_creation_refuses_an_ancestor_swapped_after_validation(tmp_path, monkeypatch):
    """The opened directory, not its raceable pathname, owns authorization."""
    project = tmp_path / "project"
    spec_dir = project / "copy"
    spec_dir.mkdir(parents=True)
    external_parent = tmp_path / "external"
    external_spec = external_parent / "copy"
    external_spec.mkdir(parents=True)
    original_project = tmp_path / "original-project"
    real_verified = routes._verified_spec_dir
    swapped = False

    def _verify_then_swap(path):
        nonlocal swapped
        verified = real_verified(path)
        if path == spec_dir and verified is not None and not swapped:
            project.rename(original_project)
            project.symlink_to(external_parent, target_is_directory=True)
            swapped = True
        return verified

    monkeypatch.setattr(routes, "_verified_spec_dir", _verify_then_swap)

    result, identity = routes._create_spec_doc(spec_dir, "requirements.md", "# copied")

    assert result == "unsafe_dir"
    assert identity is None
    assert not (external_spec / "requirements.md").exists()


@pytest.mark.skipif(
    not routes._CAN_PIN_DIR,
    reason="descriptor-pinned duplicate cleanup is unavailable",
)
def test_duplicate_rollback_refuses_an_ancestor_swapped_after_validation(tmp_path, monkeypatch):
    """A redirected path cannot make rollback unlink an external matching inode."""
    project = tmp_path / "project"
    spec_dir = project / "copy"
    spec_dir.mkdir(parents=True)
    external_parent = tmp_path / "external"
    external_spec = external_parent / "copy"
    external_spec.mkdir(parents=True)
    external_doc = external_spec / "requirements.md"
    external_doc.write_text("# external")
    stat = external_doc.stat()
    identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    original_project = tmp_path / "original-project"
    real_verified = routes._verified_spec_dir
    swapped = False

    def _verify_then_swap(path):
        nonlocal swapped
        verified = real_verified(path)
        if path == spec_dir and verified is not None and not swapped:
            project.rename(original_project)
            project.symlink_to(external_parent, target_is_directory=True)
            swapped = True
        return verified

    monkeypatch.setattr(routes, "_verified_spec_dir", _verify_then_swap)

    routes._rollback_staged_docs(spec_dir, {"requirements.md": identity})

    assert external_doc.read_text() == "# external"


@pytest.mark.skipif(
    not routes._CAN_PIN_DIR,
    reason="descriptor-pinned duplicate cleanup is unavailable",
)
def test_duplicate_cleanup_leaves_the_empty_stage_instead_of_racing_replacement(tmp_path):
    """POSIX has no inode-bound rmdir, so safe cleanup stops at an empty stage."""
    stage = tmp_path / ".copy.duplicate-deadbeef"
    stage.mkdir()

    routes._rollback_staged_docs(stage, {})

    assert stage.is_dir()
    assert not any(stage.iterdir())


@pytest.mark.skipif(
    not routes._CAN_PIN_DIR,
    reason="descriptor-pinned duplicate staging is unavailable",
)
def test_duplicate_marker_failure_does_not_remove_a_redirected_stage(tmp_path, monkeypatch):
    """Marker unwind cannot follow a swapped ancestor into an external tree."""
    project = tmp_path / "project"
    project.mkdir()
    external_parent = tmp_path / "external"
    external_parent.mkdir()
    token = "d" * 32
    stage = project / f".copy.duplicate-{token}"
    external_stage = external_parent / stage.name
    external_stage.mkdir()
    original_project = tmp_path / "original-project"

    def _swap_then_refuse(_stage_fd, _duplicate_token):
        project.rename(original_project)
        project.symlink_to(external_parent, target_is_directory=True)
        return False

    monkeypatch.setattr(routes, "_write_duplicate_marker_at", _swap_then_refuse)

    result = routes._create_duplicate_stage(stage, token)

    assert result == "write_failed"
    assert external_stage.is_dir()
    assert (original_project / stage.name).is_dir()


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
def test_duplicate_write_failure_retains_provenance_until_documents_are_rolled_back(
    tmp_path, monkeypatch
):
    """Failure cleanup cannot create markerless staged-document residue."""
    token = "d" * 32
    stage = tmp_path / f".copy.duplicate-{token}"
    target = tmp_path / "copy"
    assert routes._create_duplicate_stage(stage, token) == ""
    real_create = routes._create_spec_doc
    calls = 0

    def _fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return "write_failed", None
        return real_create(*args, **kwargs)

    monkeypatch.setattr(routes, "_create_spec_doc", _fail_second)
    result, created = routes._write_and_publish_duplicate(
        stage,
        target,
        {"requirements.md": "# copied", "design.md": "# fails"},
        token,
    )

    assert result == "write_failed"
    assert routes._duplicate_marker_matches(stage, token)
    assert routes._rollback_staged_docs(stage, created) is True
    assert routes._duplicate_marker_matches(stage, token)


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
def test_duplicate_fsyncs_staged_directory_entries_before_publication(tmp_path, monkeypatch):
    """A durable rename cannot expose child entries that were never persisted."""
    token = "d" * 32
    stage = tmp_path / f".copy.duplicate-{token}"
    target = tmp_path / "copy"
    assert routes._create_duplicate_stage(stage, token) == ""
    stage_stat = stage.stat()
    stage_identity = (stage_stat.st_dev, stage_stat.st_ino)
    stage_synced = False
    real_fsync = routes.os.fsync

    def _record_stage_fsync(fd):
        nonlocal stage_synced
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) == stage_identity:
            stage_synced = True
        return real_fsync(fd)

    monkeypatch.setattr(routes.os, "fsync", _record_stage_fsync)

    result, _created = routes._write_and_publish_duplicate(
        stage, target, {"requirements.md": "# copied"}, token
    )

    assert result == ""
    assert stage_synced
    assert (target / "requirements.md").read_text() == "# copied"


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
def test_duplicate_refuses_a_stage_replaced_at_the_publication_boundary(tmp_path, monkeypatch):
    """The renamed directory must be the descriptor-pinned transaction stage."""
    token = "d" * 32
    stage = tmp_path / f".copy.duplicate-{token}"
    displaced_stage = tmp_path / ".displaced-transaction"
    target = tmp_path / "copy"
    assert routes._create_duplicate_stage(stage, token) == ""
    real_publish = routes._publish_staged_copy

    def _replace_then_publish(stage_dir, target_dir):
        stage_dir.rename(displaced_stage)
        stage_dir.mkdir()
        (stage_dir / "requirements.md").write_text("# attacker payload")
        return real_publish(stage_dir, target_dir)

    monkeypatch.setattr(routes, "_publish_staged_copy", _replace_then_publish)

    result, _created = routes._write_and_publish_duplicate(
        stage, target, {"requirements.md": "# intended payload"}, token
    )

    assert result == "identity_mismatch"
    assert (target / "requirements.md").read_text() == "# attacker payload"
    assert (displaced_stage / "requirements.md").read_text() == "# intended payload"


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
def test_duplicate_refuses_a_stage_replaced_before_document_creation(tmp_path, monkeypatch):
    """Copied documents are written only through the reservation's stage inode."""
    token = "d" * 32
    stage = tmp_path / f".copy.duplicate-{token}"
    displaced_stage = tmp_path / ".displaced-transaction"
    target = tmp_path / "copy"
    assert routes._create_duplicate_stage(stage, token) == ""
    expected_identity = routes._duplicate_stage_identity(stage, token)
    assert expected_identity is not None
    real_create = routes._create_spec_doc

    def _replace_then_create(*args, **kwargs):
        stage.rename(displaced_stage)
        stage.mkdir()
        (stage / routes._DUPLICATE_MARKER).write_text(token)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(routes, "_create_spec_doc", _replace_then_create)

    result, _created = routes._write_and_publish_duplicate(
        stage,
        target,
        {"requirements.md": "# intended payload"},
        token,
        expected_identity,
    )

    assert result == "identity_mismatch"
    assert not (stage / "requirements.md").exists()
    assert (displaced_stage / routes._DUPLICATE_MARKER).read_text() == token


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires atomic no-replace directory publication",
)
def test_startup_recovery_publishes_a_complete_duplicate_staged_by_a_dead_process(
    tmp_path, monkeypatch
):
    """A crash after all documents are staged must not hide the copy forever.

    Custom ``base_path`` roots are not part of folder discovery, so dropping the
    reservation would strand the files while their name remains unusable.
    """
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    documents = {"requirements.md": "# copied", "design.md": ""}
    for fname, text in documents.items():
        (stage / fname).write_text(text)
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **_stage_identity(stage),
                    "documents": {
                        fname: routes._sha256_text(text) for fname, text in documents.items()
                    },
                },
            }
        }
    )

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" in loaded
    assert routes._DUPLICATING not in loaded["copy"]
    assert not stage.exists()
    assert (target / "requirements.md").read_text() == "# copied"
    assert (target / "design.md").read_text() == ""


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires atomic no-replace directory publication",
)
def test_startup_recovery_refuses_a_stage_replaced_at_the_publication_boundary(
    tmp_path, monkeypatch
):
    """Recovery must publish the inode whose marker and manifest it validated."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    displaced_stage = target.parent / ".displaced-transaction"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    (stage / "requirements.md").write_text("# intended payload")
    identity = _stage_identity(stage)
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **identity,
                    "documents": {
                        "requirements.md": routes._sha256_text("# intended payload"),
                    },
                },
            }
        }
    )
    real_publish = routes._publish_staged_copy

    def _replace_then_publish(stage_dir, target_dir):
        stage_dir.rename(displaced_stage)
        stage_dir.mkdir()
        (stage_dir / "requirements.md").write_text("# attacker payload")
        return real_publish(stage_dir, target_dir)

    monkeypatch.setattr(routes, "_publish_staged_copy", _replace_then_publish)

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" not in loaded
    assert (target / "requirements.md").read_text() == "# attacker payload"
    assert (displaced_stage / "requirements.md").read_text() == "# intended payload"


def test_startup_recovery_does_not_adopt_a_competing_target_after_identity_check_crash(
    tmp_path, monkeypatch
):
    """An ambiguous post-rename crash remains reserved instead of adopting files."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    displaced_stage = target.parent / ".displaced-transaction"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    (stage / "requirements.md").write_text("# intended payload")
    identity = _stage_identity(stage)
    stage.rename(displaced_stage)
    target.mkdir()
    (target / "requirements.md").write_text("# attacker payload")
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **identity,
                    "documents": {
                        "requirements.md": routes._sha256_text("# intended payload"),
                    },
                },
            }
        }
    )

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert routes._DUPLICATING in loaded["copy"]
    assert (target / "requirements.md").read_text() == "# attacker payload"
    assert (displaced_stage / routes._DUPLICATE_MARKER).read_text() == token


def test_startup_recovery_preserves_a_spec_with_unproven_duplicate_metadata(tmp_path, monkeypatch):
    """Untrusted reservation metadata is not proof that the entry is disposable."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = tmp_path / "custom" / "real-spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.md").write_text("# keep me")
    token = "d" * 32
    routes._save_index(
        {
            "real-spec": {
                "working_dir": str(tmp_path),
                "spec_dir": str(spec_dir),
                "status": "planning",
                "slot_key": "spec-builder-real-spec-deadbeef",
                "approvals": {"requirements": {"hash": "a" * 64}},
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(spec_dir.parent / f".real-spec.duplicate-{token}"),
                    "documents": {
                        "requirements.md": routes._sha256_text("# keep me"),
                    },
                },
            }
        }
    )

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "real-spec" in loaded
    assert routes._DUPLICATING not in loaded["real-spec"]
    assert loaded["real-spec"]["approvals"] == {"requirements": {"hash": "a" * 64}}
    assert (spec_dir / "requirements.md").read_text() == "# keep me"


def test_startup_recovery_preserves_an_unproven_reservation_with_no_files(tmp_path, monkeypatch):
    """Well-shaped metadata alone cannot prove that recovery may delete a spec."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "offline" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    approvals = {"requirements": {"hash": "a" * 64}}
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                "approvals": approvals,
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    "documents": {
                        "requirements.md": routes._sha256_text("# copied"),
                    },
                },
            }
        }
    )

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" in loaded
    assert routes._DUPLICATING not in loaded["copy"]
    assert loaded["copy"]["approvals"] == approvals


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires atomic no-replace directory publication",
)
@pytest.mark.parametrize("target_exists_before_recovery", [False, True])
def test_startup_recovery_discards_duplicate_when_an_external_target_wins(
    tmp_path, monkeypatch, target_exists_before_recovery
):
    """A proven duplicate must not adopt a competing writer's target files."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    (stage / "requirements.md").write_text("# copied")
    if target_exists_before_recovery:
        target.mkdir()
        (target / "requirements.md").write_text("# external")
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **_stage_identity(stage),
                    "documents": {
                        "requirements.md": routes._sha256_text("# copied"),
                    },
                },
            }
        }
    )

    def _concurrent_writer_wins(stage_dir, target_dir):
        assert not target_exists_before_recovery
        target_dir.mkdir()
        (target_dir / "requirements.md").write_text("# external")
        return "conflict"

    monkeypatch.setattr(routes, "_publish_staged_copy", _concurrent_writer_wins)

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" not in loaded
    assert (target / "requirements.md").read_text() == "# external"
    assert stage.is_dir()
    assert not any(stage.iterdir())


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires descriptor-pinned cleanup",
)
@pytest.mark.parametrize("external_target", [False, True])
def test_startup_recovery_cleanup_stays_on_the_proven_stage_inode(
    tmp_path, monkeypatch, external_target
):
    """Cleanup cannot unlink files from a replacement at the reserved stage name."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    displaced_stage = target.parent / ".displaced-transaction"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    (stage / "requirements.md").write_text("# copied")
    if external_target:
        target.mkdir()
        (target / "requirements.md").write_text("# external")
    documents = {"requirements.md": routes._sha256_text("# copied")}
    if not external_target:
        documents["design.md"] = routes._sha256_text("# missing")
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **_stage_identity(stage),
                    "documents": documents,
                },
            }
        }
    )
    real_clear = routes._clear_duplicate_stage_documents_at

    def _replace_then_clear(dir_fd, marker_token, manifest):
        stage.rename(displaced_stage)
        stage.mkdir()
        (stage / "requirements.md").write_text("# unrelated")
        return real_clear(dir_fd, marker_token, manifest)

    monkeypatch.setattr(routes, "_clear_duplicate_stage_documents_at", _replace_then_clear)

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" not in loaded
    assert (stage / "requirements.md").read_text() == "# unrelated"
    assert not (displaced_stage / "requirements.md").exists()
    assert (displaced_stage / routes._DUPLICATE_MARKER).read_text() == token
    if external_target:
        assert (target / "requirements.md").read_text() == "# external"


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires atomic no-replace directory publication",
)
def test_startup_recovery_keeps_a_duplicate_published_by_a_dead_process(tmp_path, monkeypatch):
    """The exact post-rename/pre-index crash window keeps the visible copy."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    target.mkdir(parents=True)
    (target / routes._DUPLICATE_MARKER).write_text(token)
    (target / "requirements.md").write_text("# copied")
    stage = target.parent / f".copy.duplicate-{token}"
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **_stage_identity(target),
                    "documents": {
                        "requirements.md": routes._sha256_text("# copied"),
                    },
                },
            }
        }
    )

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" in loaded
    assert routes._DUPLICATING not in loaded["copy"]
    assert (target / "requirements.md").read_text() == "# copied"


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires atomic no-replace directory publication",
)
def test_startup_recovery_discards_an_incomplete_duplicate_stage_from_a_dead_process(
    tmp_path, monkeypatch
):
    """A marker-provenanced partial stage is transaction residue, not user data."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    (stage / "requirements.md").write_text("# only the first document landed")
    documents = {
        "requirements.md": routes._sha256_text("# only the first document landed"),
        "design.md": routes._sha256_text("# never written"),
    }
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **_stage_identity(stage),
                    "documents": documents,
                },
            }
        }
    )

    real_save = routes._save_index

    def _assert_cleanup_order(index):
        if "copy" not in index:
            assert routes._duplicate_marker_matches(stage, token)
            assert not (stage / "requirements.md").exists()
        real_save(index)

    monkeypatch.setattr(routes, "_save_index", _assert_cleanup_order)

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert "copy" not in loaded
    assert stage.is_dir()
    assert not any(stage.iterdir()), "the abandoned transaction left hidden copied documents"


@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate recovery requires descriptor-pinned cleanup",
)
@pytest.mark.parametrize("external_target", [False, True])
def test_startup_recovery_preserves_a_mismatched_staged_document(
    tmp_path, monkeypatch, external_target
):
    """A marker proves the stage directory, not ownership of replaced files."""
    _redirect_state(monkeypatch, tmp_path)
    token = "d" * 32
    target = tmp_path / "custom" / "copy"
    stage = target.parent / f".copy.duplicate-{token}"
    stage.mkdir(parents=True)
    (stage / routes._DUPLICATE_MARKER).write_text(token)
    moved_file = tmp_path / "user-requirements.md"
    moved_file.write_text("# user file moved into the abandoned stage")
    moved_file.replace(stage / "requirements.md")
    if external_target:
        target.mkdir()
        (target / "requirements.md").write_text("# external target")
    routes._save_index(
        {
            "copy": {
                "working_dir": str(tmp_path),
                "spec_dir": str(target),
                "status": "planning",
                "slot_key": "spec-builder-copy-deadbeef",
                routes._DUPLICATING: {
                    "owner": "1234:deadbeef",
                    "at": 1.0,
                    "token": token,
                    "stage_dir": str(stage),
                    **_stage_identity(stage),
                    "documents": {
                        "requirements.md": routes._sha256_text("# copied"),
                    },
                },
            }
        }
    )

    routes._recover_abandoned_reservations()
    loaded = routes._load_index()

    assert routes._DUPLICATING in loaded["copy"]
    assert (stage / "requirements.md").read_text() == "# user file moved into the abandoned stage"
    if external_target:
        assert (target / "requirements.md").read_text() == "# external target"


# ── gap 1: documents are reviewable through their redacted rendering ─────────


def test_duplicate_doc_create_only_succeeds_while_the_file_is_absent(tmp_path):
    """O_EXCL makes an external writer the winner rather than its victim."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()

    result, identity = routes._create_spec_doc(spec_dir, "design.md", "# fresh")
    assert result == "" and identity is not None
    assert (spec_dir / "design.md").read_text() == "# fresh"

    result, identity = routes._create_spec_doc(spec_dir, "design.md", "# second")
    assert result == "conflict" and identity is None
    assert (spec_dir / "design.md").read_text() == "# fresh"


@pytest.mark.skipif(
    not routes._CAN_PIN_DIR,
    reason="duplicate document writes require descriptor-relative directory pinning",
)
def test_duplicate_doc_create_refuses_a_spec_directory_swapped_for_a_symlink(tmp_path):
    """Same attack the sentinel writers refuse: an agent replaces its own indexed
    directory with a symlink, and a path-based write then lands user-authored text
    somewhere else entirely."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    attacker = tmp_path / "attacker-spec"
    os.symlink(real, attacker)

    assert routes._create_spec_doc(attacker, "design.md", "# text")[0] == "unsafe_dir"
    assert not (real / "design.md").exists()


def test_duplicate_doc_create_retries_short_writes(tmp_path, monkeypatch):
    """A successful save means the complete UTF-8 payload reached the temporary
    file; a regular-file write is allowed to accept only a prefix."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    real_write = routes.os.write
    writes: list[int] = []

    def _short_write(fd, data):
        chunk = data[:2]
        writes.append(len(chunk))
        return real_write(fd, chunk)

    monkeypatch.setattr(routes.os, "write", _short_write)
    content = "complete payload"

    assert routes._create_spec_doc(spec_dir, "design.md", content)[0] == ""
    assert (spec_dir / "design.md").read_text() == content
    assert len(writes) > 1


# ── gap 5: duplicate ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_copies_the_documents_into_a_fresh_spec(tmp_path, monkeypatch):
    """The recovery path rename cannot serve: a spec whose NAME is wrong once it
    already has a branch or history. The copy takes the documents and nothing
    else, so it gets its own slot key and therefore its own conversation."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# reqs", "design.md": "# design"})
    state, _ = _state_for("live")
    sent: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda st, slot, msg: sent.append(msg))

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 201, body
    copy_dir = Path(body["spec_dir"])
    assert copy_dir.name == "live-copy"
    assert (copy_dir / "requirements.md").read_text() == "# reqs"
    assert (copy_dir / "design.md").read_text() == "# design"
    assert not (copy_dir / routes._DUPLICATE_MARKER).exists()

    index = routes._load_index()
    assert index["live"]["spec_dir"] != index["live-copy"]["spec_dir"]
    assert index["live-copy"]["slot_key"] != index["live"]["slot_key"], "shared a transcript"
    # A duplicate never inherits a worktree: branching off someone's repo is not a
    # copy operation, and it is an opt-in at create time.
    assert index["live-copy"]["worktree_branch"] == ""
    # The fresh conversation is told what it is looking at, and told not to rewrite it.
    assert len(sent) == 1 and "copy of 'live'" in sent[0] and "Do NOT rewrite" in sent[0]


@pytest.mark.asyncio
async def test_duplicate_refuses_while_the_source_agent_is_writing(tmp_path, monkeypatch):
    """A live agent turn can rewrite phase files while they are being copied."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# reqs"})
    state, slots = _state_for("live")
    slots[routes._slot_key("live")].running = True
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: None)

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "agent_running"
    assert "live-copy" not in routes._load_index()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_refuses_a_mixed_source_document_snapshot(tmp_path, monkeypatch):
    """Every copied phase file must belong to one stable source snapshot."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, _working_dir = _seed_spec(
        tmp_path,
        files={"requirements.md": "# requirements v1", "design.md": "# design v1"},
    )
    real_read = routes._read_spec_text
    changed = False

    def _change_plan_between_reads(directory, fname):
        nonlocal changed
        text = real_read(directory, fname)
        if fname == "requirements.md" and not changed:
            changed = True
            (spec_dir / "requirements.md").write_text("# requirements v2")
            (spec_dir / "design.md").write_text("# design v2")
        return text

    monkeypatch.setattr(routes, "_read_spec_text", _change_plan_between_reads)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: None)

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "mixed-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "spec_changed_during_duplicate"
    assert "mixed-copy" not in routes._load_index()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_redacts_the_returned_spec_directory(tmp_path, monkeypatch):
    """A credential-like project path must not cross the HTTP response boundary raw."""
    client = _make_client(monkeypatch, tmp_path)
    secret_root = tmp_path / "secret-segment"
    _seed_spec(secret_root, files={"requirements.md": "# copied"})
    monkeypatch.setattr(
        routes,
        "_redact",
        lambda text: text.replace("secret-segment", "[REDACTED]"),
    )
    monkeypatch.setattr(routes, "_dispatch_turn", lambda _state, _slot, _message: None)

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 201, body
    assert "secret-segment" not in body["spec_dir"]
    assert "[REDACTED]" in body["spec_dir"]
    assert Path(routes._load_index()["live-copy"]["spec_dir"]).is_dir()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_publishes_recovery_provenance_before_index_reservation(
    tmp_path, monkeypatch
):
    """A crash after reservation must leave proof that recovery may discard it."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# copied"})
    real_mutate = routes._mutate_index

    async def _assert_provenance(fn):
        changed = await real_mutate(fn)
        if changed and getattr(fn, "__name__", "") == "_insert":
            meta = routes._load_index()["live-copy"]
            held = meta[routes._DUPLICATING]
            assert routes._duplicate_marker_matches(Path(held["stage_dir"]), held["token"])
        return changed

    monkeypatch.setattr(routes, "_mutate_index", _assert_provenance)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: None)

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 201, body


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_cancelled_duplicate_finishes_its_published_transaction(tmp_path, monkeypatch):
    """Request cancellation cannot hide a published copy behind our reservation."""
    _redirect_state(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# copied"})
    state, _ = _state_for("live")
    published = threading.Event()
    release_write = threading.Event()
    real_write = routes._write_and_publish_duplicate

    def _publish_then_block(*args, **kwargs):
        result = real_write(*args, **kwargs)
        assert result[0] == ""
        published.set()
        assert release_write.wait(default_timeout()), "cancelled transaction was never released"
        return result

    async def _body(_request):
        return {"new_name": "cancelled-copy"}

    async def _fresh(_request, _name, _body):
        return routes._load_index()["live"]

    monkeypatch.setattr(routes, "_require_auth", lambda _request: None)
    monkeypatch.setattr(routes, "_read_json", _body)
    monkeypatch.setattr(routes, "_pinned_entry", _fresh)
    monkeypatch.setattr(routes, "_write_and_publish_duplicate", _publish_then_block)
    request = cast(
        web.Request,
        types.SimpleNamespace(match_info={"name": "live"}, app={"state": state}),
    )

    handler = asyncio.create_task(routes._handle_duplicate(request))
    try:
        await async_wait_until(published.is_set, describe=lambda: "the duplicate never published")
        handler.cancel()
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await handler
    finally:
        release_write.set()

    meta = routes._load_index()["cancelled-copy"]
    target = Path(meta["spec_dir"])
    assert routes._DUPLICATING not in meta
    assert (target / "requirements.md").read_text() == "# copied"
    assert not (target / routes._DUPLICATE_MARKER).exists()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_preserves_an_existing_empty_document(tmp_path, monkeypatch):
    """An empty phase file is data, not the same thing as a missing document."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# reqs", "design.md": ""})
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: None)

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 201, body
    copy_dir = Path(body["spec_dir"])
    assert (copy_dir / "requirements.md").read_text() == "# reqs"
    assert (copy_dir / "design.md").is_file()
    assert (copy_dir / "design.md").read_text() == ""


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_refuses_an_existing_document_that_cannot_be_read(tmp_path, monkeypatch):
    """An oversized source document must not be silently omitted from a copy."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(
        tmp_path,
        files={
            "requirements.md": "# copied",
            "design.md": "x" * (routes._MAX_SPEC_BYTES + 1),
        },
    )
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args: None)

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "incomplete-copy"},
        )
        assert resp.status == 409
        body = await resp.json()
    finally:
        await client.close()

    assert body["code"] == "spec_document_unreadable"
    assert "incomplete-copy" not in routes._load_index()
    assert not (working_dir / ".kiro" / "specs" / "incomplete-copy").exists()


@pytest.mark.asyncio
async def test_duplicate_refuses_a_name_already_in_use(tmp_path, monkeypatch):
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path, files={"requirements.md": "# reqs"})

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        same = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live"},
        )
        same_body = await same.json()
        bad = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "../escape"},
        )
        bad_body = await bad.json()
    finally:
        await client.close()

    assert same.status == 409 and same_body["code"] == "spec_exists"
    assert bad.status == 400 and bad_body["code"] == "invalid_name"


@pytest.mark.asyncio
async def test_duplicate_refuses_a_spec_with_no_documents_yet(tmp_path, monkeypatch):
    """Nothing to copy is a refusal, not an empty spec created as a side effect."""
    client = _make_client(monkeypatch, tmp_path)
    _seed_spec(tmp_path)

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "empty-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "nothing_to_copy"
    assert "empty-copy" not in routes._load_index()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_reserves_its_name_before_populating_the_destination(tmp_path, monkeypatch):
    """A concurrent create must not win the destination index entry while the
    duplicate is already copying documents into that same directory."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(tmp_path, files={"requirements.md": "# copied"})
    state, _ = _state_for("live")
    real_create = routes._create_spec_doc
    save_started = threading.Event()
    release_save = threading.Event()
    hold_failures: list[str] = []

    def _held_save(*args, **kwargs):
        save_started.set()
        if not release_save.wait(default_timeout()):
            # Raised inside the handler this would surface only as a 500; record it
            # so the test fails by name below instead.
            hold_failures.append("the test never released the held save")
            raise TimeoutError(hold_failures[-1])
        return real_create(*args, **kwargs)

    monkeypatch.setattr(routes, "_create_spec_doc", _held_save)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args, **_kwargs: None)

    client.app["state"] = state
    await client.start_server()
    try:
        duplicate_request = asyncio.create_task(
            client.post(
                f"{_BASE}/specs/live/duplicate",
                json={**_spec_identity(), "new_name": "shared-name"},
            )
        )
        await async_wait_until(save_started.is_set, describe=lambda: "the save never started")
        create = await client.post(
            f"{_BASE}/specs",
            json={
                "name": "shared-name",
                "working_dir": str(working_dir),
                "spec_type": "feature",
                "description": "concurrent create",
                "use_worktree": False,
            },
        )
        create_body = await create.json()
        release_save.set()
        duplicate = await duplicate_request
        assert not hold_failures, hold_failures[0]
        duplicate_body = await duplicate.json()
    finally:
        release_save.set()
        await client.close()

    assert create.status == 409 and create_body["code"] == "spec_exists"
    assert duplicate.status == 201, duplicate_body
    copy_dir = Path(duplicate_body["spec_dir"])
    assert (copy_dir / "requirements.md").read_text() == "# copied"
    assert routes._load_index()["shared-name"]["spec_dir"] == str(copy_dir)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_refuses_when_the_destination_changes_after_reservation(
    tmp_path, monkeypatch
):
    """The index reservation and copied files must always name one directory."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(tmp_path, files={"requirements.md": "# copied"})
    moved_base = tmp_path / "moved-specs"
    moved_base.mkdir()
    state, _ = _state_for("live")
    real_mutate = routes._mutate_index
    moved = False

    async def _move_settings_after_reservation(fn):
        nonlocal moved
        changed = await real_mutate(fn)
        if changed and not moved and "live-copy" in routes._load_index():
            moved = True
            routes._save_settings({"base_path": str(moved_base), "model": ""})
        return changed

    monkeypatch.setattr(routes, "_mutate_index", _move_settings_after_reservation)
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *_args, **_kwargs: None)

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "live-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 409 and body["code"] == "spec_destination_changed"
    assert "live-copy" not in routes._load_index()
    assert not (moved_base / "live-copy").exists()
    assert not (working_dir / ".kiro" / "specs" / "live-copy").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_rolls_back_only_files_it_created_after_a_partial_failure(
    tmp_path, monkeypatch
):
    """A failed copy must not become discoverable as an incomplete spec."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(
        tmp_path,
        files={"requirements.md": "# copied", "design.md": "# fails"},
    )
    state, _ = _state_for("live")
    real_create = routes._create_spec_doc
    calls = 0

    def _fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return "write_failed", None
        return real_create(*args, **kwargs)

    monkeypatch.setattr(routes, "_create_spec_doc", _fail_second)

    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "partial-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    target = working_dir / ".kiro" / "specs" / "partial-copy"
    assert resp.status == 400 and body["code"] == "doc_write_failed"
    assert "partial-copy" not in routes._load_index()
    assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_preserves_a_published_copy_when_index_finalization_fails(
    tmp_path, monkeypatch
):
    """A post-rename failure remains recoverable instead of poisoning retries."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(tmp_path, files={"requirements.md": "# copied"})
    real_mutate = routes._mutate_index

    async def _fail_finish(fn):
        if getattr(fn, "__name__", "") == "_finish":
            return False
        return await real_mutate(fn)

    monkeypatch.setattr(routes, "_mutate_index", _fail_finish)

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "recoverable-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    target = working_dir / ".kiro" / "specs" / "recoverable-copy"
    assert resp.status == 409 and body["code"] == "spec_changed_during_create"
    assert (target / "requirements.md").read_text() == "# copied"
    assert (target / routes._DUPLICATE_MARKER).is_file()
    assert routes._DUPLICATING in routes._load_index()["recoverable-copy"]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_contains_an_index_finalization_exception(tmp_path, monkeypatch):
    """A post-publication index error must return the recoverable-copy response."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(tmp_path, files={"requirements.md": "# copied"})
    real_mutate = routes._mutate_index

    async def _raise_finish(fn):
        if getattr(fn, "__name__", "") == "_finish":
            raise OSError("index storage unavailable")
        return await real_mutate(fn)

    monkeypatch.setattr(routes, "_mutate_index", _raise_finish)

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "recoverable-copy"},
        )
        assert resp.status == 409
        body = await resp.json()
    finally:
        await client.close()

    target = working_dir / ".kiro" / "specs" / "recoverable-copy"
    assert body["code"] == "spec_changed_during_create"
    assert (target / "requirements.md").read_text() == "# copied"
    assert (target / routes._DUPLICATE_MARKER).is_file()
    assert routes._DUPLICATING in routes._load_index()["recoverable-copy"]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not routes._CAN_PUBLISH_DIR_NOREPLACE,
    reason="duplicate publication requires atomic no-replace directory publication",
)
async def test_duplicate_keeps_its_committed_copy_when_slot_setup_is_refused(tmp_path, monkeypatch):
    """Session arbitration must not delete a copy already committed to the index."""
    client = _make_client(monkeypatch, tmp_path)
    _, working_dir = _seed_spec(tmp_path, files={"requirements.md": "# copied"})

    async def _refuse_slot(*_args, **_kwargs):
        return None

    monkeypatch.setattr(routes, "_ensure_worker_slot", _refuse_slot)

    client.app["state"] = _state_for("live")[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/live/duplicate",
            json={**_spec_identity(), "new_name": "committed-copy"},
        )
        body = await resp.json()
    finally:
        await client.close()

    target = working_dir / ".kiro" / "specs" / "committed-copy"
    assert resp.status == 409 and body["code"] == "slot_owned_by_another_app"
    assert (target / "requirements.md").read_text() == "# copied"
    assert "committed-copy" in routes._load_index()
