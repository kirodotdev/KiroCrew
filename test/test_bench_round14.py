"""Round-14 review findings.

Two defects, both on paths the diff added:

1. A durable report was truncated before its replacement existed, so a reused
   ``--stem`` traded a stored baseline for whatever an interrupted write left behind.
2. ``_measurable_count`` read its nested container with a raw ``.get(field, {})``,
   which defaults only for a MISSING key -- a report carrying
   ``"session_measurable": null`` raised ``AttributeError`` out of the one command
   whose job is to say whether two runs are comparable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew.eval.bench.run import compare_reports
from kiro_crew.eval.bench.safepath import UnsafePathError, write_text_atomic_nofollow


def _report(measurable: object) -> dict:
    """A pair-comparable report whose per-cut-off count is *measurable*."""
    return {
        "corpus": {"fingerprint": "abc"},
        "config": {
            "ingest": {"granularity": "session"},
            "retrieval": {"k_values": [5]},
            "search_backend": "sqlite_cosine",
            "embedder": "qwen3-embedding:0.6b@1024",
            "environment": {"python": "3.12.0"},
        },
        "metrics": {
            "session": {"recall_all": {"5": 0.5}},
            "session_measurable": measurable,
            "session_population": {"5": "deadbeef"},
        },
    }


def test_null_measurable_container_refuses_instead_of_crashing() -> None:
    """A present-but-null count container must reach the not-comparable branch.

    Pre-fix this raised ``AttributeError`` from ``None.get`` -- neither
    ``bench_cmd`` (``BenchRefusal``/``OSError``) nor ``_compare`` (``_BenchError``)
    catches that, so the user got a traceback instead of an answer.
    """
    out = compare_reports(_report(None), _report(None), k=5)
    assert "session_measurable" in out
    assert "cannot be shown" in out


@pytest.mark.parametrize("bad", [[], "12", 12, {"5": "x"}])
def test_non_dict_measurable_container_is_not_comparable(bad: object) -> None:
    """Any non-mapping container collapses to the same refusal, never a crash."""
    out = compare_reports(_report(bad), _report(bad), k=5)
    assert "session_measurable" in out


def test_failed_write_leaves_the_previous_report_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that dies part-way must not consume the baseline that was there.

    ``fsync`` stands in for the realistic killers -- ENOSPC, SIGKILL, a full
    ``/tmp`` -- because it is the last step before the rename and the only one that
    can be made to fail deterministically.
    """
    dest = tmp_path / "baseline.json"
    previous = json.dumps({"recall_all": {"5": 0.7139}})
    dest.write_text(previous, encoding="utf-8")

    def boom(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        write_text_atomic_nofollow(dest, '{"recall_all": {"5": 0.0}}', what="JSON report")

    assert dest.read_text(encoding="utf-8") == previous
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "baseline.json"]
    assert leftovers == [], f"temporary file not cleaned up: {leftovers}"


def test_successful_write_replaces_the_previous_report(tmp_path: Path) -> None:
    """The happy path still publishes the new bytes and leaves no temporary behind."""
    dest = tmp_path / "baseline.json"
    dest.write_text("old", encoding="utf-8")

    write_text_atomic_nofollow(dest, "new", what="JSON report")

    assert dest.read_text(encoding="utf-8") == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["baseline.json"]


def test_first_write_creates_the_file(tmp_path: Path) -> None:
    """A destination that does not exist yet is the normal case, not a refusal."""
    dest = tmp_path / "fresh.json"

    write_text_atomic_nofollow(dest, "{}", what="JSON report")

    assert dest.read_text(encoding="utf-8") == "{}"


def test_failed_first_write_leaves_no_empty_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed first write must not leave a 0-byte file that looks like a report.

    The alias check opens the destination WITHOUT ``O_CREAT`` for exactly this: a
    reader cannot tell an empty report from a truncated one, so none is written.
    """
    dest = tmp_path / "fresh.json"

    def boom(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        write_text_atomic_nofollow(dest, "{}", what="JSON report")

    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_still_refuses_a_symlink_at_the_name(tmp_path: Path) -> None:
    """Atomicity must not cost the symlink refusal.

    ``os.replace`` over a link writes nothing through it, but it does discard a name
    the caller never asked to touch, so the refusal stays.
    """
    target = tmp_path / "elsewhere.json"
    target.write_text("protected", encoding="utf-8")
    link = tmp_path / "report.json"
    link.symlink_to(target)

    with pytest.raises(UnsafePathError, match="symbolic link"):
        write_text_atomic_nofollow(link, "clobber", what="JSON report")

    assert target.read_text(encoding="utf-8") == "protected"


def test_atomic_write_still_refuses_a_hardlink_alias(tmp_path: Path) -> None:
    """A second name for the same inode is refused on the descriptor, as before."""
    target = tmp_path / "elsewhere.json"
    target.write_text("protected", encoding="utf-8")
    alias = tmp_path / "report.json"
    os.link(target, alias)

    with pytest.raises(UnsafePathError, match="hard link"):
        write_text_atomic_nofollow(alias, "clobber", what="JSON report")

    assert target.read_text(encoding="utf-8") == "protected"
