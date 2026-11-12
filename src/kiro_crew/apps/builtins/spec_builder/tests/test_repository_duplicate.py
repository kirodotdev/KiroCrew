"""Refusal paths of Spec Builder's duplicate transaction (``repository_duplicate``).

The route tests in ``spec_builder/tests/test_routes_duplicate.py`` drive a duplicate
end to end. This file names the refusals those flows rarely reach, one helper at a time:
a document name outside the editable set, an oversized document, a stage directory
that is missing or carries another transaction's marker, a recovery manifest that does
not name complete digests, and a publication whose stage and target are not siblings.
Each refusal is the fail-closed answer a crash-safe copy depends on, so each is pinned
by the value it returns rather than by the flow around it.

Everything runs under ``tmp_path``; nothing spawns a process or touches the app's state
files. The descriptor-pinned helpers need ``dir_fd`` support, which Windows lacks, so
those cases skip there (the platform's refusal is the ``unsupported_platform`` answer).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes as r

_PINNED = pytest.mark.skipif(not r._CAN_PIN_DIR, reason="needs dir_fd directory pinning")
_PUBLISHABLE = pytest.mark.skipif(
    not r._CAN_PUBLISH_DIR_NOREPLACE, reason="needs an atomic no-replace directory rename"
)

_DIGEST = "0" * 64


def test_a_document_outside_the_editable_set_is_refused_before_any_write(tmp_path: Path):
    assert r._create_spec_doc(tmp_path, "notes.md", "# notes") == ("not_editable", None)
    assert list(tmp_path.iterdir()) == []


def test_an_oversized_document_is_refused_before_any_write(tmp_path: Path):
    text = "x" * (r._MAX_SPEC_BYTES + 1)
    assert r._create_spec_doc(tmp_path, "requirements.md", text) == ("too_large", None)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "documents",
    [
        pytest.param([], id="not-a-mapping"),
        pytest.param({}, id="empty"),
        pytest.param({"notes.md": _DIGEST}, id="not-an-editable-document"),
        pytest.param({"requirements.md": "0" * 63}, id="short-digest"),
        pytest.param({"requirements.md": 0}, id="digest-not-text"),
        pytest.param({"requirements.md": "z" * 64}, id="digest-not-hex"),
    ],
)
def test_a_recovery_manifest_must_name_complete_document_digests(documents: object):
    assert r._duplicate_manifest_is_valid(documents) is False


def test_a_complete_recovery_manifest_is_valid():
    assert r._duplicate_manifest_is_valid({"requirements.md": _DIGEST}) is True


def test_an_invalid_manifest_matches_no_payload_without_reading_the_directory():
    # The descriptor is never consulted: -1 would raise on any read.
    assert r._duplicate_documents_match_at(-1, {"notes.md": _DIGEST}) is False


def test_a_missing_stage_has_no_marker_identity_or_removal(tmp_path: Path):
    missing = tmp_path / ".copy.duplicate-token"
    assert r._duplicate_marker_matches(missing, "token") is False
    assert r._duplicate_stage_identity(missing, "token") is None
    r._remove_duplicate_marker(missing, "token")
    assert not missing.exists()


@_PINNED
def test_a_stage_carrying_another_marker_has_no_identity(tmp_path: Path):
    stage = tmp_path / ".copy.duplicate-mine"
    stage.mkdir()
    (stage / r._DUPLICATE_MARKER).write_text("theirs", encoding="ascii")
    assert r._duplicate_stage_identity(stage, "mine") is None
    assert r._duplicate_marker_matches(stage, "mine") is False


@_PINNED
def test_a_marker_is_kept_when_the_stage_is_not_the_recorded_inode(tmp_path: Path):
    stage = tmp_path / ".copy.duplicate-mine"
    stage.mkdir()
    marker = stage / r._DUPLICATE_MARKER
    marker.write_text("mine", encoding="ascii")
    r._remove_duplicate_marker(stage, "mine", expected_identity=(-1, -1))
    assert marker.read_text(encoding="ascii") == "mine"


@_PUBLISHABLE
def test_a_stage_whose_parent_is_missing_is_never_created(tmp_path: Path):
    stage = tmp_path / "absent-parent" / ".copy.duplicate-token"
    assert r._create_duplicate_stage(stage, "token") == "write_failed"
    assert not stage.parent.exists()


@_PUBLISHABLE
def test_a_stage_and_target_that_are_not_siblings_are_never_published(tmp_path: Path):
    stage = tmp_path / "one" / ".copy.duplicate-token"
    target = tmp_path / "two" / "copy"
    assert r._publish_staged_copy(stage, target) == "unsupported_platform"


@_PUBLISHABLE
def test_a_stage_that_does_not_verify_is_never_published(tmp_path: Path):
    stage = tmp_path / ".copy.duplicate-token"
    target = tmp_path / "copy"
    assert r._publish_staged_copy(stage, target) == "unsafe_dir"
    assert not target.exists()
