"""``ConversationLog`` preserves the images its rows reference.

The unit contract lives in ``test_chat_attachments.py``; this file pins the write
boundary -- that the row landing on disk names the copy, that ``append_if_absent``
still recognises an already-persisted row after the rewrite, and that deleting the
session reclaims the images it showed.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.chat_attachments import attachments_dir
from kiro_crew.history import ConversationLog

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


@pytest.fixture()
def png(tmp_path):
    source = tmp_path / "scratch" / "shot.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(PNG_BYTES)
    return source


def _rows(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("_type") != "metadata"
    ]


def test_appended_assistant_row_names_the_stored_copy(tmp_path, png):
    log = ConversationLog(base_dir=tmp_path)
    log.append("thread1", "assistant", f"done:\n\n![shot]({png})")

    (row,) = _rows(tmp_path / "thread1.jsonl")
    stored = next(attachments_dir(tmp_path, "thread1").iterdir())
    assert str(stored) in row["content"]
    assert str(png) not in row["content"]
    assert stored.read_bytes() == PNG_BYTES
    assert png.read_bytes() == PNG_BYTES


def test_user_rows_are_left_alone(tmp_path, png):
    """A path the user typed names a file of their own, not ours to duplicate."""
    log = ConversationLog(base_dir=tmp_path)
    text = f"look at ![mine]({png})"
    log.append("thread1", "user", text)

    (row,) = _rows(tmp_path / "thread1.jsonl")
    assert row["content"] == text
    assert not attachments_dir(tmp_path, "thread1").exists()


def test_append_if_absent_still_dedups_after_the_rewrite(tmp_path, png):
    """The rewrite must not make an already-persisted row look new.

    ``append_if_absent`` compares the candidate against what is on disk, and the
    disk copy carries the rewritten path -- so comparing the original text would
    append the same message twice.
    """
    log = ConversationLog(base_dir=tmp_path)
    text = f"![shot]({png})"

    assert log.append_if_absent("thread1", "assistant", text) is True
    assert log.append_if_absent("thread1", "assistant", text) is False

    assert len(_rows(tmp_path / "thread1.jsonl")) == 1
    assert len(list(attachments_dir(tmp_path, "thread1").iterdir())) == 1


def test_an_id_carrying_append_is_deduped_by_id_when_the_image_is_gone(tmp_path, png):
    """A queued append whose scratch image has vanished must not duplicate the row.

    The slot save landed this message first, with the image rewritten to its stored
    copy. By the time the fire-and-forget ``append_if_absent`` runs the agent's
    scratch file is gone, so its own rewrite fails open to the original path and
    the body differs from the row on disk. The id plus the text agreeing modulo
    the preserved image is what recognises the row as this message; nothing is
    appended.
    """
    log = ConversationLog(base_dir=tmp_path)
    text = f"![shot]({png})"
    mid = "m-0123456789abcdef"

    log.append("thread1", "assistant", text, mid=mid)  # the slot save's landing
    png.unlink()  # the agent process is gone and scratch with it

    assert log.append_if_absent("thread1", "assistant", text, mid=mid) is False

    rows = _rows(tmp_path / "thread1.jsonl")
    assert len(rows) == 1
    assert str(png) not in rows[0]["content"]  # the stored row keeps the durable path


def test_an_id_carrying_append_with_different_text_still_lands(tmp_path, png):
    """The image allowance is not identity-only dedup: a same-id row whose TEXT
    differs is uncorroborated (``meta.mid`` is caller-suppliable) and still lands.
    """
    log = ConversationLog(base_dir=tmp_path)
    mid = "m-0123456789abcdef"
    log.append("thread1", "assistant", f"first ![shot]({png})", mid=mid)
    assert log.append_if_absent("thread1", "assistant", f"second ![shot]({png})", mid=mid) is True
    assert len(_rows(tmp_path / "thread1.jsonl")) == 2


def test_an_id_carrying_append_under_another_id_still_lands(tmp_path, png):
    """Identity-based dedup skips only the SAME id; a new occurrence still lands."""
    log = ConversationLog(base_dir=tmp_path)
    text = f"![shot]({png})"

    log.append("thread1", "assistant", text, mid="m-earlier0000000001")
    assert log.append_if_absent("thread1", "assistant", text, mid="m-newer000000000002") is True
    assert [r["meta"]["mid"] for r in _rows(tmp_path / "thread1.jsonl")] == [
        "m-earlier0000000001",
        "m-newer000000000002",
    ]


def test_delete_session_removes_the_attachments(tmp_path, png):
    log = ConversationLog(base_dir=tmp_path)
    log.append("thread1", "assistant", f"![shot]({png})")
    target = attachments_dir(tmp_path, "thread1")
    assert target.is_dir()

    assert log.delete_session("thread1") is True

    assert not (tmp_path / "thread1.jsonl").exists()
    assert not target.exists()
    # The agent's own file is not the session's to delete.
    assert png.exists()


def test_the_copy_runs_under_the_session_lock(tmp_path, png, monkeypatch):
    """Otherwise a concurrent ``delete_session`` reclaims it before the row lands.

    ``delete_session`` removes the attachments directory under this same lock, so
    a copy made outside it can be deleted between the copy and the append --
    persisting a row that names a file already gone, which is the defect the
    feature exists to remove.
    """
    import kiro_crew.history as history_mod

    log = ConversationLog(base_dir=tmp_path)
    real = history_mod.ConversationLog._persist_inline_attachments
    held: list[bool] = []

    def spy(self, key, role, content):
        held.append(self._file_lock(key)._is_owned())
        return real(self, key, role, content)

    monkeypatch.setattr(history_mod.ConversationLog, "_persist_inline_attachments", spy)
    log.append("thread1", "assistant", f"![shot]({png})")

    assert held and all(held), held


def test_delete_is_refused_when_attachments_cannot_be_moved_aside(tmp_path, png):
    """Step 1 fails -> nothing has changed: transcript and images both intact,
    and the ordinary delete still works afterwards."""
    import kiro_crew.history_projection as projection

    log = ConversationLog(base_dir=tmp_path)
    log.append("thread1", "assistant", f"![shot]({png})")
    monkeypatch = pytest.MonkeyPatch()

    def refuse(*_a, **_k):
        raise PermissionError("busy")

    monkeypatch.setattr(projection, "stage_attachments_removal", refuse)
    try:
        assert log.delete_session("thread1") is False
    finally:
        monkeypatch.undo()

    assert (tmp_path / "thread1.jsonl").exists()
    assert attachments_dir(tmp_path, "thread1").is_dir()
    assert log.delete_session("thread1") is True
    assert not (tmp_path / "thread1.jsonl").exists()
    assert not attachments_dir(tmp_path, "thread1").exists()


def test_a_failed_transcript_unlink_puts_the_attachments_back(tmp_path, png, monkeypatch):
    """Step 2 fails -> step 1 is undone, so the retained transcript's image
    references resolve again. The transcript never outlives its pictures."""
    from pathlib import Path

    log = ConversationLog(base_dir=tmp_path)
    log.append("thread1", "assistant", f"![shot]({png})")
    before = sorted(p.name for p in attachments_dir(tmp_path, "thread1").iterdir())
    real_unlink = Path.unlink

    def refuse(self, *a, **k):
        if self.name == "thread1.jsonl":
            raise PermissionError("busy")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", refuse)
    assert log.delete_session("thread1") is False
    monkeypatch.undo()

    assert (tmp_path / "thread1.jsonl").exists()
    assert sorted(p.name for p in attachments_dir(tmp_path, "thread1").iterdir()) == before
    assert not [p for p in tmp_path.iterdir() if ".trash-" in p.name]


def test_a_purge_residue_does_not_fail_the_delete(tmp_path, png, monkeypatch):
    """Step 3 fails -> the transcript is gone and nothing references the bytes, so
    the delete is reported as done and the orphan is left for an operator."""
    import kiro_crew.chat_attachments as mod

    log = ConversationLog(base_dir=tmp_path)
    log.append("thread1", "assistant", f"![shot]({png})")
    monkeypatch.setattr(mod.shutil, "rmtree", lambda *_a, **_k: None)

    assert log.delete_session("thread1") is True

    assert not (tmp_path / "thread1.jsonl").exists()
    assert not attachments_dir(tmp_path, "thread1").exists()
    orphans = [p for p in tmp_path.iterdir() if ".trash-" in p.name]
    assert len(orphans) == 1 and orphans[0].is_dir()


def test_two_sessions_referencing_one_image_keep_separate_copies(tmp_path, png):
    """Attachments are per session, so one session's delete cannot blank another's."""
    log = ConversationLog(base_dir=tmp_path)
    log.append("thread1", "assistant", f"![shot]({png})")
    log.append("thread2", "assistant", f"![shot]({png})")

    assert log.delete_session("thread1") is True

    assert not attachments_dir(tmp_path, "thread1").exists()
    surviving = list(attachments_dir(tmp_path, "thread2").iterdir())
    assert len(surviving) == 1
    (row,) = _rows(tmp_path / "thread2.jsonl")
    assert str(surviving[0]) in row["content"]
