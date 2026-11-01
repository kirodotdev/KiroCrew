"""A heartbeat pass rewrites only the lines that held a finished task.

Everything else in HEARTBEAT.md -- the user's headings, notes, commented-out
(paused) tasks and the tasks it keeps -- survives a pass byte for byte, and the
task text handed to the agent carries no list or checkbox marker.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kiro_crew.heartbeat as hb_mod
from kiro_crew.heartbeat import _HEADER, HeartbeatService, _extract_tasks


async def _run_pass(
    path: Path, monkeypatch: pytest.MonkeyPatch, keep: frozenset[str] | set[str] = frozenset()
) -> list[str]:
    seen: list[str] = []

    async def on_task(text: str, deliver: str) -> str:
        seen.append(text)
        return "not yet HEARTBEAT_KEEP" if text in keep else "done"

    monkeypatch.setattr(hb_mod, "heartbeat_path", lambda: path)
    await HeartbeatService(memory=MagicMock(), on_task=on_task)._process_heartbeat_file()
    return seen


@pytest.mark.asyncio
async def test_user_notes_and_paused_task_survive_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "HEARTBEAT.md"
    before = (
        "# My checks\n"
        "\n"
        "<!-- Notes: run these on weekdays only. -->\n"
        "<!-- - paused: rotate the keys -->\n"
        "## Daily\n"
        "- check the pipeline\n"
        "<!--\n"
        "- paused: renew the cert  <!-- deliver:slack -->\n"
        "-->\n"
        "- watch the release  <!-- deliver:dashboard -->\n"
        "\n"
        "### Done\n"
    )
    path.write_text(before, encoding="utf-8")

    seen = await _run_pass(path, monkeypatch, keep={"watch the release"})

    assert sorted(seen) == ["check the pipeline", "watch the release"]
    assert path.read_text(encoding="utf-8") == before.replace("- check the pipeline\n", "")


@pytest.mark.asyncio
async def test_a_file_the_heartbeat_wrote_round_trips_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "HEARTBEAT.md"
    before = _HEADER + "- first\n- second  <!-- deliver:slack -->\n"
    path.write_text(before, encoding="utf-8")

    await _run_pass(path, monkeypatch, keep={"first", "second"})

    assert path.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_crlf_lines_are_kept_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "HEARTBEAT.md"
    before = b"# Tasks\r\n<!-- note -->\r\n- done one\r\n- kept one\r\n"
    path.write_bytes(before)

    await _run_pass(path, monkeypatch, keep={"kept one"})

    assert path.read_bytes() == b"# Tasks\r\n<!-- note -->\r\n- kept one\r\n"


@pytest.mark.asyncio
async def test_finished_task_keeps_the_comment_it_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "HEARTBEAT.md"
    path.write_text("- check CI <!-- note\n- paused\n--> - after\n", encoding="utf-8")

    seen = await _run_pass(path, monkeypatch, keep={"after"})

    assert sorted(seen) == ["after", "check CI"]
    after = path.read_text(encoding="utf-8")
    assert after == "<!-- note\n- paused\n--> - after\n"
    assert _extract_tasks(after) == [("after", "")]


@pytest.mark.asyncio
async def test_same_text_tasks_are_told_apart_by_their_deliver_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "HEARTBEAT.md"
    path.write_text(
        "- deploy  <!-- deliver:slack -->\n- deploy  <!-- deliver:dashboard -->\n",
        encoding="utf-8",
    )

    async def on_task(text: str, deliver: str) -> str:
        return "not yet HEARTBEAT_KEEP" if deliver == "slack" else "done"

    monkeypatch.setattr(hb_mod, "heartbeat_path", lambda: path)
    await HeartbeatService(memory=MagicMock(), on_task=on_task)._process_heartbeat_file()

    assert path.read_text(encoding="utf-8") == "- deploy  <!-- deliver:slack -->\n"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (b"# Notes\n- done", b"# Notes\n"),
        (b"# Notes\r\n- done", b"# Notes\r\n"),
        (b"# Notes\n- done\n- kept", b"# Notes\n- kept"),
    ],
)
@pytest.mark.asyncio
async def test_dropping_the_unterminated_last_line_keeps_the_line_before_it_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: bytes, after: bytes
) -> None:
    path = tmp_path / "HEARTBEAT.md"
    path.write_bytes(before)

    await _run_pass(path, monkeypatch, keep={"kept"})

    assert path.read_bytes() == after
    hb_mod.append_heartbeat_task("- new task", path=path)
    assert ("new task", "") in _extract_tasks(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_a_split_crlf_line_keeps_crlf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "HEARTBEAT.md"
    path.write_bytes(b"<!-- a\r\nb --> - done <!-- c\r\nd -->\r\n")

    await _run_pass(path, monkeypatch)

    assert path.read_bytes() == b"<!-- a\r\nb -->\r\n<!-- c\r\nd -->\r\n"


@pytest.mark.parametrize(
    "line",
    [
        "- task",
        "* task",
        "+ task",
        "1. task",
        "12) task",
        "- [ ] task",
        "- [x] task",
        "- [X] task",
        "* [ ] task",
        "* [x] task",
        "+ [ ] task",
        "1. [x] task",
        "[ ] task",
        "-\ttask",
    ],
)
def test_list_and_checkbox_markers_are_stripped(line: str) -> None:
    assert _extract_tasks(line + "\n") == [("task", "")]


@pytest.mark.parametrize("line", ["-", "* [ ]", "1.", "- [x]"])
def test_a_bare_marker_is_not_a_task(line: str) -> None:
    assert _extract_tasks(line) == []


@pytest.mark.parametrize("line", ["-flag on", "[x]y", "1.5 hours"])
def test_text_that_only_looks_like_a_marker_is_kept(line: str) -> None:
    assert _extract_tasks(line) == [(line, "")]
