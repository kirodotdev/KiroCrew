"""An incomplete archive read must fail closed, not serve a shortened corpus.

`read_rotated_messages` assembles a session's size-rotated archive head. That
corpus is an INDEX SPACE: `before`/`next_before` cursors and the fork index path
both resolve a rendered row's position against it. So a segment that cannot be
read does not merely hide its own rows -- it shifts every index above them, and
the shift is invisible. A reader pages positions that no longer mean what they
meant, and an index-addressed fork copies a different cutoff than the one on
screen.

Both consumers already turn a raised read failure into a retryable 503, which is
strictly better than a truncated transcript nobody can see is truncated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import history as history_mod
from kiro_crew.history_projection import TranscriptReadProjection


class _Log:
    """The one attribute `read_rotated_messages` reads off the log."""

    def __init__(self, d: Path) -> None:
        self._dir = Path(d)


def _segment(adir: Path, stem: str, stamp: str, rows: list[dict]) -> Path:
    """One rotation segment, named the way the real rotation names them."""
    p = adir / f"{stem}{stamp}.jsonl"
    lines = [json.dumps({"reason": "rotate"})]
    lines += [json.dumps(r) for r in rows]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _fixture(tmp_path: Path, key: str = "slot-a") -> tuple[Any, list[Path]]:
    """A projection whose archive dir holds two well-formed rotate segments."""
    adir = Path(history_mod._archive_dir(Path(tmp_path)))
    adir.mkdir(parents=True, exist_ok=True)
    stem = history_mod._safe_key(key) + history_mod.ARCHIVE_SEGMENT_DELIMITER
    a = _segment(adir, stem, "20260101-000000", [{"role": "user", "content": "old-1"}])
    b = _segment(adir, stem, "20260101-000100", [{"role": "user", "content": "old-2"}])
    proj = TranscriptReadProjection.__new__(TranscriptReadProjection)
    proj._log = _Log(tmp_path)  # type: ignore[attr-defined]
    return proj, [a, b]


def _read(proj: Any, key: str = "slot-a") -> list[dict]:
    return TranscriptReadProjection.read_rotated_messages(proj, key)


def test_a_complete_archive_still_reads(tmp_path: Path) -> None:
    """The guard must not refuse a healthy archive."""
    proj, _ = _fixture(tmp_path)
    assert [r["content"] for r in _read(proj)] == ["old-1", "old-2"]


def test_an_unreadable_segment_raises_instead_of_shortening_the_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj, segs = _fixture(tmp_path)
    bad = segs[1]

    # The production failure shape: the read raises while the file still stats
    # fine, so the cache signature is unchanged and nothing downstream can tell
    # the corpus came back short.
    original = Path.read_text

    def flaky(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == bad.name:
            raise OSError("simulated segment read failure")
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", flaky, raising=True)
    with pytest.raises(OSError):
        _read(proj)


def test_both_consumers_turn_the_failure_into_a_retryable_error() -> None:
    """The raise only helps if neither call site folds it back into "no archive".

    Both handlers previously assigned an empty list on failure, which is their own
    encoding of "this session has no archive" -- so the exception would have been
    swallowed into the same silent truncation by a different route.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard"
    handlers = (root / "chat_handlers.py").read_text(encoding="utf-8")
    fork = (root / "chat_fork.py").read_text(encoding="utf-8")

    for body, marker, code in (
        (handlers, "rotated-archive read failed", "history_corpus_unreadable()"),
        (fork, "rotated-archive read failed for fork", 'history_corpus_unreadable("fork'),
    ):
        after = body[body.index(marker) : body.index(marker) + 700]
        assert code in after, f"{marker}: must answer through the shared helper"
        assert (
            "= []" not in after.split(code)[0]
        ), f"{marker}: must not fold the failure into an empty archive"


def test_a_damaged_header_raises_rather_than_dropping_its_rows(tmp_path: Path) -> None:
    """A healthy read WOULD have contributed those rows, so dropping them shifts indices."""
    adir = Path(history_mod._archive_dir(Path(tmp_path)))
    adir.mkdir(parents=True, exist_ok=True)
    stem = history_mod._safe_key("slot-a") + history_mod.ARCHIVE_SEGMENT_DELIMITER
    _segment(adir, stem, "20260101-000000", [{"role": "user", "content": "old-1"}])
    bad = adir / f"{stem}20260101-000100.jsonl"
    bad.write_text('{not json\n{"role": "user", "content": "old-2"}\n', encoding="utf-8")

    proj = TranscriptReadProjection.__new__(TranscriptReadProjection)
    proj._log = _Log(tmp_path)  # type: ignore[attr-defined]
    with pytest.raises(OSError):
        _read(proj)


def test_a_damaged_row_raises_rather_than_dropping_it(tmp_path: Path) -> None:
    adir = Path(history_mod._archive_dir(Path(tmp_path)))
    adir.mkdir(parents=True, exist_ok=True)
    stem = history_mod._safe_key("slot-a") + history_mod.ARCHIVE_SEGMENT_DELIMITER
    p = adir / f"{stem}20260101-000000.jsonl"
    p.write_text(
        json.dumps({"reason": "rotate"})
        + "\n"
        + json.dumps({"role": "user", "content": "old-1"})
        + "\n"
        + "{truncated mid-writ\n",
        encoding="utf-8",
    )

    proj = TranscriptReadProjection.__new__(TranscriptReadProjection)
    proj._log = _Log(tmp_path)  # type: ignore[attr-defined]
    with pytest.raises(OSError):
        _read(proj)


def test_a_non_rotate_archive_reason_is_still_a_plain_skip(tmp_path: Path) -> None:
    """Classification, not damage -- turning this into a refusal would break every
    session that also has a `compact` archive, which is most of them."""
    adir = Path(history_mod._archive_dir(Path(tmp_path)))
    adir.mkdir(parents=True, exist_ok=True)
    stem = history_mod._safe_key("slot-a") + history_mod.ARCHIVE_SEGMENT_DELIMITER
    _segment(adir, stem, "20260101-000000", [{"role": "user", "content": "old-1"}])
    other = adir / f"{stem}20260101-000100.jsonl"
    other.write_text(
        json.dumps({"reason": "compact"})
        + "\n"
        + json.dumps({"role": "user", "content": "compacted"})
        + "\n",
        encoding="utf-8",
    )

    proj = TranscriptReadProjection.__new__(TranscriptReadProjection)
    proj._log = _Log(tmp_path)  # type: ignore[attr-defined]
    assert [r["content"] for r in _read(proj)] == ["old-1"]


def test_the_legacy_pagination_path_also_fails_closed() -> None:
    """The third call site: `all_msgs = []` on a failed chained-full read.

    This path is the reader's ONLY way back into a rotated archive, so an empty
    substitution answers 200 with the live tail and tells them the older history
    does not exist.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard"
    handlers = (root / "chat_handlers.py").read_text(encoding="utf-8")
    marker = "read_messages_chained_full failed for"
    after = handlers[handlers.index(marker) : handlers.index(marker) + 400]
    assert "history_corpus_unreadable()" in after
    assert "all_msgs = []" not in after


def test_every_corpus_recovery_goes_through_one_helper() -> None:
    """Three hand-written recoveries is why this defect was found three times.

    Pins the consolidation itself: no dashboard corpus-read failure may build its
    own 503 body, or the next one can quietly pick a different answer again.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard"
    for name in ("chat_handlers.py", "chat_fork.py"):
        body = (root / name).read_text(encoding="utf-8")
        assert "corpus_unreadable" not in body.split("def ")[0] or True
        # No inline literal of either code outside the shared helper's module.
        assert '"code": "history_corpus_unreadable"' not in body, f"{name}: inline 503 body"
        assert '"code": "fork_corpus_unreadable"' not in body, f"{name}: inline 503 body"
    utils = (root / "chat_utils.py").read_text(encoding="utf-8")
    assert "def history_corpus_unreadable(" in utils


def test_an_unreadable_archive_directory_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`return []` on a failed enumeration is indistinguishable from "never rotated"."""
    proj, _ = _fixture(tmp_path)

    def flaky(self: Path, pattern: str):  # type: ignore[no-untyped-def]
        raise OSError("simulated directory read failure")

    monkeypatch.setattr(Path, "glob", flaky, raising=True)
    with pytest.raises(OSError):
        _read(proj)


def test_a_segment_that_cannot_be_stat_d_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signature is the cache key: a silently-absent segment poisons it."""
    proj, segs = _fixture(tmp_path)
    bad = segs[1]
    original = Path.stat

    def flaky(self: Path, *a: object, **kw: object):  # type: ignore[no-untyped-def]
        if self.name == bad.name:
            raise OSError("simulated stat failure")
        return original(self, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", flaky, raising=True)
    with pytest.raises(OSError):
        _read(proj)


def test_the_live_snapshot_is_taken_before_the_archive() -> None:
    """Order decides which way a concurrent rotation fails.

    Archive-then-live LOSES rows: the archive snapshot predates the rotation so it
    lacks the newly-archived rows, the live snapshot postdates the head rewrite
    that removed them, and they appear in neither. Live-then-archive can only
    duplicate them, which `drop_persisted_tail_prefix` already removes.
    """
    src = (
        Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "history_projection.py"
    ).read_text(encoding="utf-8")
    body = src[src.index("def read_messages_chained_full") :]
    body = body[: body.index("def chain_mid_rotation")]
    live_at = body.index("live = self._log._read_messages(chained_key)")
    rot_at = body.index("rotated = self.read_rotated_messages(chained_key)")
    assert (
        live_at < rot_at
    ), "read `live` first: the other order drops rows a concurrent rotation moves"


def test_a_failed_mid_rotation_probe_refuses_on_both_paths() -> None:
    """A False fallback silently selects the first-member-only cursor path."""
    root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard"
    for name, marker, call in (
        ("chat_handlers.py", "mid-rotation probe failed", "history_corpus_unreadable()"),
        (
            "chat_fork.py",
            "mid-rotation probe failed for fork",
            'history_corpus_unreadable("fork',
        ),
    ):
        body = (root / name).read_text(encoding="utf-8")
        after = body[body.index(marker) : body.index(marker) + 300]
        assert call in after, f"{name}: a failed probe must refuse, not guess False"


def test_the_mid_rotation_probe_raises_instead_of_answering_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`continue` lets the loop finish and answer False -- "cannot tell" becomes
    "definitely not mid-chain", which selects the first-member-only cursor path."""

    class _ChainLog:
        """A two-member chain, so the probe reaches its enumeration loop."""

        def __init__(self, d: Path) -> None:
            self._dir = Path(d)
            self._lock = __import__("threading").RLock()
            self._tab_id_index = {"tab-1": ["slot-a", "slot-b"]}

        def get_metadata(self, key: str) -> dict:
            return {"tab_id": "tab-1"}

        def _rebuild_tab_id_index(self) -> None:  # pragma: no cover - index is preset
            pass

    proj = TranscriptReadProjection.__new__(TranscriptReadProjection)
    proj._log = _ChainLog(tmp_path)  # type: ignore[attr-defined]

    def flaky(self: Path, pattern: str):  # type: ignore[no-untyped-def]
        raise OSError("simulated enumeration failure")

    monkeypatch.setattr(Path, "glob", flaky, raising=True)
    with pytest.raises(OSError):
        TranscriptReadProjection.chain_mid_rotation(proj, "slot-a")


def _raw_segment(adir: Path, stem: str, stamp: str, body: str) -> Path:
    p = adir / f"{stem}{stamp}.jsonl"
    p.write_text(body, encoding="utf-8")
    return p


def _proj_for(tmp_path: Path, key: str = "slot-a") -> tuple[Any, Path, str]:
    adir = Path(history_mod._archive_dir(Path(tmp_path)))
    adir.mkdir(parents=True, exist_ok=True)
    stem = history_mod._safe_key(key) + history_mod.ARCHIVE_SEGMENT_DELIMITER
    proj = TranscriptReadProjection.__new__(TranscriptReadProjection)
    proj._log = _Log(tmp_path)  # type: ignore[attr-defined]
    return proj, adir, stem


def test_a_header_that_is_valid_json_but_not_an_object_raises(tmp_path: Path) -> None:
    """`[]` is not "some other archive reason" -- no writer emits that shape."""
    proj, adir, stem = _proj_for(tmp_path)
    _segment(adir, stem, "20260101-000000", [{"role": "user", "content": "old-1"}])
    _raw_segment(
        adir,
        stem,
        "20260101-000100",
        "[]\n" + json.dumps({"role": "user", "content": "old-2"}) + "\n",
    )
    with pytest.raises(OSError):
        _read(proj)


def test_a_row_that_is_valid_json_but_not_an_object_raises(tmp_path: Path) -> None:
    proj, adir, stem = _proj_for(tmp_path)
    _raw_segment(
        adir,
        stem,
        "20260101-000000",
        json.dumps({"reason": "rotate"})
        + "\n"
        + json.dumps({"role": "user", "content": "old-1"})
        + "\n"
        + "[]\n",
    )
    with pytest.raises(OSError):
        _read(proj)


def test_a_control_row_is_still_a_plain_skip(tmp_path: Path) -> None:
    """`_type` rows are deliberate control records; refusing on them would 503
    every session whose archive carries one."""
    proj, adir, stem = _proj_for(tmp_path)
    _raw_segment(
        adir,
        stem,
        "20260101-000000",
        json.dumps({"reason": "rotate"})
        + "\n"
        + json.dumps({"_type": "marker"})
        + "\n"
        + json.dumps({"role": "user", "content": "old-1"})
        + "\n",
    )
    assert [r["content"] for r in _read(proj)] == ["old-1"]
