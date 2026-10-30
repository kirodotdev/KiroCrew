"""An undecodable ``comments.json`` sidecar reads as no comments.

``_load_comments`` is documented as tolerant, and a sidecar that is not valid
JSON reads as empty. A sidecar that is not valid UTF-8 (a hand edit or a backup
restored in a legacy code page) must read as empty too: every comment read and
write loads the sidecar first, so a raise there would leave the artifact's
comments neither listable nor recoverable by posting a new one.
"""

from __future__ import annotations

import uuid

import pytest

from kiro_crew.artifacts import ArtifactComment, ArtifactStore

_SLUG = "corrupt-comments"
#: A UTF-16 BOM and a latin-1 ``é``: neither byte sequence is valid UTF-8.
_UNDECODABLE = b'\xff\xfe[{"body": "caf\xe9"}]'


@pytest.fixture
def store(tmp_path):
    s = ArtifactStore(root=tmp_path / "artifacts")
    s.create(name="Corrupt comments", content="<p>Hello</p>", slug=_SLUG)
    return s


def _sidecar(store: ArtifactStore):
    return store._artifact_dir(_SLUG) / "comments.json"


def _comment(body: str) -> ArtifactComment:
    return ArtifactComment(
        id=str(uuid.uuid4()),
        origin="local",
        scope="private",
        author="alice",
        body=body,
        thread_id="",
        created_at="2026-10-26T00:00:00Z",
        updated_at="2026-10-26T00:00:00Z",
    )


def test_malformed_json_sidecar_reads_as_empty_control(store):
    """Control: a sidecar that is not valid JSON reads as empty."""
    _sidecar(store).write_text('[{"id": ', encoding="utf-8")
    assert store.list_comments(_SLUG) == []


def test_undecodable_sidecar_reads_as_empty(store):
    _sidecar(store).write_bytes(_UNDECODABLE)
    try:
        comments = store.list_comments(_SLUG)
    except UnicodeDecodeError as exc:
        pytest.fail(f"list_comments raised on an undecodable sidecar: {exc}")
    assert comments == []


def test_undecodable_sidecar_accepts_a_new_comment(store):
    _sidecar(store).write_bytes(_UNDECODABLE)
    try:
        added = store.add_comment(_SLUG, _comment("recovered"))
    except UnicodeDecodeError as exc:
        pytest.fail(f"add_comment raised on an undecodable sidecar: {exc}")
    assert [c.id for c in store.list_comments(_SLUG)] == [added.id]
