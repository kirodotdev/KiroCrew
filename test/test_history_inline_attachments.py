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


def test_delete_is_refused_while_attachments_remain(tmp_path, png):
    """The transcript must not outlive its own reclamation failure.

    Attachments are served content, like the search index's copy of the message
    text, so they are removed BEFORE the transcript and a failure aborts the
    delete. Unlinking first would report success while leaving served images with
    no transcript left to find them from.
    """
    import kiro_crew.history_projection as projection

    log = ConversationLog(base_dir=tmp_path)
    log.append("thread1", "assistant", f"![shot]({png})")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(projection, "remove_attachments", lambda *_a, **_k: False)
    try:
        assert log.delete_session("thread1") is False
    finally:
        monkeypatch.undo()

    assert (tmp_path / "thread1.jsonl").exists()
    assert attachments_dir(tmp_path, "thread1").is_dir()
    # Retryable: nothing was destroyed, so the ordinary delete still works.
    assert log.delete_session("thread1") is True
    assert not (tmp_path / "thread1.jsonl").exists()


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
