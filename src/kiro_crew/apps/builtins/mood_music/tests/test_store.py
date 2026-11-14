"""Mood Music's JSON store: bounded records, feedback, and no silent overwrite."""

from __future__ import annotations

from pathlib import Path

from kiro_crew.apps.builtins.mood_music.backend import store as store_mod
from kiro_crew.apps.builtins.mood_music.backend.store import MoodStore


def _pick(title: str = "Song") -> dict:
    return {
        "title": title,
        "artist": "A",
        "why": "w",
        "tags": [],
        "source": "youtube",
        "video_id": "dQw4w9WgXcQ",
    }


def _store(tmp_path: Path) -> MoodStore:
    return MoodStore(tmp_path / "state.json", now=lambda: 1000.0)


def test_an_empty_store_reads_empty(tmp_path: Path) -> None:
    assert _store(tmp_path).snapshot() == {
        "moments": [],
        "queue": [],
        "feedback": [],
        "last_source": "",
    }


def test_moments_are_newest_first_and_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "MAX_MOMENTS", 3)
    s = _store(tmp_path)
    for mood in ("a", "b", "c", "d"):
        s.add_moment(mood)
    assert [m["mood"] for m in s.snapshot()["moments"]] == ["d", "c", "b"]


def test_a_queue_attaches_its_songs_and_tags_to_the_moment(tmp_path: Path) -> None:
    s = _store(tmp_path)
    moment = s.add_moment("想念")
    queue = s.set_queue(
        [_pick("x"), _pick("y")], source="r1", moment_id=moment["id"], mood_tags=["温柔"]
    )
    snap = s.snapshot()
    assert [p["title"] for p in snap["queue"]] == ["x", "y"]
    assert all(p["feedback"] == "" and p["id"] for p in queue)
    assert snap["last_source"] == "r1"
    assert snap["moments"][0]["songs"] == [
        {"title": "x", "artist": "A"},
        {"title": "y", "artist": "A"},
    ]
    assert snap["moments"][0]["tags"] == ["温柔"]


def test_feedback_is_kept_after_the_queue_moves_on(tmp_path: Path) -> None:
    s = _store(tmp_path)
    (first,) = s.set_queue([_pick("liked one")], source="r1")
    marked = s.set_feedback(first["id"], "liked")
    assert marked is not None and marked["feedback"] == "liked"
    s.set_queue([_pick("next")], source="r2")
    feedback = s.snapshot()["feedback"]
    assert [(f["title"], f["verdict"]) for f in feedback] == [("liked one", "liked")]


def test_changing_or_clearing_a_verdict_leaves_one_entry_or_none(tmp_path: Path) -> None:
    s = _store(tmp_path)
    (pick,) = s.set_queue([_pick()], source="r1")
    s.set_feedback(pick["id"], "liked")
    s.set_feedback(pick["id"], "skipped")
    assert [f["verdict"] for f in s.snapshot()["feedback"]] == ["skipped"]
    s.set_feedback(pick["id"], "")
    assert s.snapshot()["feedback"] == []


def test_feedback_on_an_unknown_pick_changes_nothing(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.set_feedback("0" * 32, "liked") is None
    assert not (tmp_path / "state.json").exists()


def test_a_damaged_file_is_moved_aside_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    s = _store(tmp_path)
    assert s.snapshot()["moments"] == []
    assert (tmp_path / "state.json.bad").read_text(encoding="utf-8") == "{not json"
    s.add_moment("calm")
    assert [m["mood"] for m in s.snapshot()["moments"]] == ["calm"]
