"""The Mastermind memory tree: nodes, views, zoom, persistence and summaries."""

from __future__ import annotations

import asyncio
import re

import pytest

from kiro_crew.mastermind.constants import (
    KIND_AGENT,
    KIND_USER,
    MESSAGES_FILE,
    NODE_BYTES,
    NODES_FILE,
)
from kiro_crew.mastermind.prompts import NOT_SUMMARIZED
from kiro_crew.mastermind.summarizer import Summarizer
from kiro_crew.mastermind.tree import MemoryTree, roots, span, utf8_len, view_high_for
from kiro_crew.mastermind.worker import TreeWorker
from kiro_crew.testing.wait import await_until

LONG = "word " * 200  # over NODE_BYTES: needs a summary call


def _lines(tree: MemoryTree) -> list[str]:
    return tree.render().split("\n")[1:-1]


def test_short_messages_are_their_own_lines_at_once(tmp_path):
    tree = MemoryTree(tmp_path)
    tree.append(KIND_USER, "hello")
    tree.append(KIND_AGENT, "hi there")
    assert _lines(tree) == ["0+1|user: hello", "1+1|agent: hi there"]
    # the pair fits too, so its parent is built without a call
    assert tree.node((1, 0)) == "user: hello\nagent: hi there"
    assert tree.jobs(4) == []


def test_a_long_message_waits_for_its_summary(tmp_path):
    tree = MemoryTree(tmp_path)
    tree.append(KIND_USER, LONG)
    assert _lines(tree) == [f"0+1|{NOT_SUMMARIZED}"]
    jobs = tree.jobs(4)
    assert [(j.lv, j.i) for j in jobs] == [(0, 0)]
    assert "<input>\nuser: word" in jobs[0].prompt
    assert tree.jobs(4) == []  # busy: not offered twice
    tree.finish(0, 0, "user asked about words")
    assert _lines(tree) == ["0+1|user asked about words"]


def test_finish_clips_a_line_that_is_too_long(tmp_path):
    tree = MemoryTree(tmp_path)
    tree.append(KIND_USER, LONG)
    (job,) = tree.jobs(1)
    tree.finish(job.lv, job.i, "x" * (NODE_BYTES * 2))
    assert utf8_len(tree.node((0, 0)) or "") == NODE_BYTES


def test_release_offers_a_job_again(tmp_path):
    tree = MemoryTree(tmp_path)
    tree.append(KIND_USER, LONG)
    (job,) = tree.jobs(1)
    tree.release(job.lv, job.i)
    assert [(j.lv, j.i) for j in tree.jobs(1)] == [(0, 0)]


def test_the_view_merges_old_lines_past_its_high(tmp_path):
    # each message alone fits a line, but no pair does: every merge is a summary
    tree = MemoryTree(tmp_path, high=4000)
    for n in range(64):
        tree.append(KIND_USER if n % 2 == 0 else KIND_AGENT, f"message number {n:03d} " + "x" * 300)
    # summaries of merged pairs: anything short
    while jobs := tree.jobs(8):
        for job in jobs:
            tree.finish(job.lv, job.i, f"summary of {job.lv}/{job.i}")
    parts = tree.view.parts
    assert tree.view.size <= 4000
    # the lines tile the chat from 0 to its end, in order
    nxt = 0
    for part in parts:
        first, last = span(part)
        assert first == nxt
        nxt = last + 1
    assert nxt == tree.count
    # the oldest lines cover more than the newest
    assert parts[0][0] > parts[-1][0]
    assert parts[-1] == (0, 63)


def test_zoom_opens_a_line_down_to_the_message(tmp_path):
    tree = MemoryTree(tmp_path)
    for n in range(4):
        tree.append(KIND_USER, f"m{n}")
    assert tree.zoom(0, 4) == "0+2|user: m0 user: m1\n2+2|user: m2 user: m3"
    assert tree.zoom(2, 1) == "2+0|user: m2"
    assert tree.zoom(3, 2).startswith("No line 3+2")
    assert "roots: 0+4" in tree.zoom(0, 8)


def test_zoom_pages_a_long_message(tmp_path):
    tree = MemoryTree(tmp_path)
    tree.append(KIND_AGENT, "a" * 25_000)
    first = tree.zoom(0, 1)
    assert first.endswith("[5000 more characters: zoom with at=20000]")
    assert tree.zoom(0, 1, at=20_000) == "0+0|agent: " + "a" * 5000


def test_roots_cover_the_count():
    assert roots(0) == []
    assert roots(5) == [(2, 0), (0, 4)]
    assert roots(8) == [(3, 0)]


def test_a_reload_keeps_messages_nodes_and_view(tmp_path):
    tree = MemoryTree(tmp_path, high=600)
    for n in range(16):
        tree.append(KIND_USER, f"line {n} " + "y" * 30)
    while jobs := tree.jobs(8):
        for job in jobs:
            tree.finish(job.lv, job.i, f"s{job.lv}.{job.i}")
    before = tree.render()
    again = MemoryTree(tmp_path, high=600)
    assert again.count == 16
    assert again.render() == before
    assert again.date(3) == tree.date(3)


def test_a_torn_message_loses_only_itself_across_reloads(tmp_path):
    tree = MemoryTree(tmp_path)
    tree.append(KIND_USER, "one")
    with open(tmp_path / MESSAGES_FILE, "a", encoding="utf-8") as fh:
        fh.write('{"i": 1, "kind": "us')  # a crash mid-write
    again = MemoryTree(tmp_path)
    assert again.count == 1
    again.append(KIND_USER, "two")
    again.append(KIND_AGENT, "three")
    final = MemoryTree(tmp_path)
    assert final.count == 3
    assert final.zoom(2, 1) == "2+0|agent: three"


def test_a_torn_node_is_dropped_and_rebuilt(tmp_path):
    tree = MemoryTree(tmp_path)
    tree.append(KIND_USER, "one")
    with open(tmp_path / NODES_FILE, "a", encoding="utf-8") as fh:
        fh.write('{"l": 0, "i": 9, "te')
    again = MemoryTree(tmp_path)
    again.append(KIND_USER, "two")
    assert MemoryTree(tmp_path).node((1, 0)) == "user: one\nuser: two"


def test_nodes_past_the_kept_messages_are_dropped(tmp_path):
    tree = MemoryTree(tmp_path)
    for n in range(4):
        tree.append(KIND_USER, f"m{n}")
    assert tree.built((2, 0))
    # the messages log loses its last two lines (cut back after a crash)
    path = tmp_path / MESSAGES_FILE
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(lines[:2]), encoding="utf-8")
    again = MemoryTree(tmp_path)
    assert again.count == 2
    assert not again.built((2, 0))
    again.append(KIND_USER, "new")
    again.append(KIND_USER, "newer")
    assert again.zoom(0, 4) == "0+2|user: m0 user: m1\n2+2|user: new user: newer"


def test_recent_holds_the_last_messages_whole(tmp_path):
    tree = MemoryTree(tmp_path)
    assert tree.last_messages() == ""
    for n in range(10):
        tree.append(KIND_USER, f"message {n} " + "w" * 600)
    recent = tree.last_messages().split("\n")
    assert recent[0] == "<recent>" and recent[-1] == "</recent>"
    assert [line.split("+")[0] for line in recent[1:-1]] == ["4", "5", "6", "7", "8", "9"]
    assert recent[-2].startswith("9+0|user: message 9 www")


def test_between_messages_a_view_changes_only_by_whole_merges(tmp_path):
    tree = MemoryTree(tmp_path, high=4000)
    for n in range(40):
        tree.append(KIND_USER, f"message number {n:03d} " + "x" * 300)
    landed = changed = 0
    while jobs := tree.jobs(8):
        for job in jobs:
            before = list(tree.view.parts)
            tree.finish(job.lv, job.i, f"s{job.lv}.{job.i}")
            landed += 1
            changed += tree.view.parts != before
    # a merge lands whole (when it reaches half), not one pair per summary
    assert landed >= 20 and changed <= 3


def test_clear_forgets_everything(tmp_path):
    tree = MemoryTree(tmp_path)
    tree.append(KIND_USER, "secret")
    tree.clear()
    assert tree.count == 0 and tree.render() == "<chat>\n</chat>"
    assert MemoryTree(tmp_path).count == 0
    tree.append(KIND_USER, "fresh")
    assert MemoryTree(tmp_path).zoom(0, 1) == "0+0|user: fresh"


def test_a_lone_surrogate_does_not_lose_the_message(tmp_path):
    tree = MemoryTree(tmp_path)
    tree.append(KIND_USER, "broken \ud83d emoji")
    assert MemoryTree(tmp_path).count == 1


def test_summarizer_writes_jobs_and_keeps_the_shortest_line(tmp_path):
    prompts: list[str] = []

    async def summarize(prompt: str) -> str:
        prompts.append(prompt)
        # first answer too long, second fits
        return "z" * (NODE_BYTES + 10) if len(prompts) == 1 else "short summary"

    async def scenario() -> None:
        worker = TreeWorker(tmp_path)
        summarizer = Summarizer(worker, summarize, retry_secs=3600)
        await worker.run(lambda t: t.append(KIND_USER, LONG))
        summarizer.kick()
        await await_until(asyncio.ensure_future(summarizer.idle()), what="the summaries")
        await summarizer.close()
        assert await worker.run(lambda t: t.node((0, 0))) == "short summary"
        worker.shutdown()

    asyncio.run(scenario())
    assert re.search(r"Too long: your last line for this <input> was \d+ bytes", prompts[1])


def test_summarizer_gives_a_failed_job_back(tmp_path):
    async def summarize(prompt: str) -> str:
        raise RuntimeError("backend down")

    async def scenario() -> None:
        worker = TreeWorker(tmp_path)
        summarizer = Summarizer(worker, summarize, retry_secs=3600)
        await worker.run(lambda t: t.append(KIND_USER, LONG))
        summarizer.kick()
        await await_until(asyncio.ensure_future(summarizer.idle()), what="the summaries")
        await summarizer.close()
        assert not await worker.run(lambda t: t.built((0, 0)))
        jobs = await worker.jobs(1)
        assert [(j.lv, j.i) for j in jobs] == [(0, 0)]
        worker.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("count", [1, 2, 3, 7, 33])
def test_every_summary_job_names_its_inputs(tmp_path, count):
    tree = MemoryTree(tmp_path)
    for n in range(count):
        tree.append(KIND_USER, f"{n} " + LONG)
    seen = set()
    while jobs := tree.jobs(4):
        for job in jobs:
            assert (job.lv, job.i) not in seen
            seen.add((job.lv, job.i))
            tree.finish(job.lv, job.i, f"s{job.lv}.{job.i}")
    # every leaf and every complete pair got built
    assert all(tree.built((0, i)) for i in range(count))
    for part in roots(count):
        assert tree.built(part)


def test_a_turn_view_over_budget_keeps_its_newest_lines(tmp_path):
    tree = MemoryTree(tmp_path)
    for n in range(40):
        tree.append(KIND_USER, f"line {n:02d} " + "q" * 80)
    whole = tree.render()
    capped = tree.render(budget=1000).split("\n")
    assert len(whole) > 1000 and sum(len(x) + 1 for x in capped[2:-1]) <= 1000
    assert capped[1].startswith("(messages 0 to ") and "zoom these lines" in capped[1]
    assert capped[-2].startswith("39+1|user: line 39")
    assert tree.render(budget=10**6) == whole


@pytest.mark.parametrize(
    "window, high",
    [
        (None, 200_000),  # unknown: a 200K window is assumed
        (0, 200_000),
        (100_000, 128_000),  # the floor
        (200_000, 200_000),
        (400_000, 400_000),
        (1_000_000, 1_000_000),
        (2_000_000, 1_000_000),  # the ceiling
    ],
)
def test_the_turn_view_is_a_share_of_the_models_window(window, high):
    assert view_high_for(window) == high


def test_a_smaller_window_shrinks_the_view_and_a_larger_one_keeps_it(tmp_path):
    tree = MemoryTree(tmp_path, high=8000)
    for n in range(40):
        tree.append(KIND_USER, f"message number {n:03d} " + "x" * 300)
    while jobs := tree.jobs(8):
        for job in jobs:
            tree.finish(job.lv, job.i, f"s{job.lv}.{job.i}")
    lines = len(tree.view.parts)
    tree.set_high(2000)
    assert tree.view.size <= 2000 and len(tree.view.parts) < lines
    shrunk = list(tree.view.parts)
    tree.set_high(50_000)
    assert tree.view.parts == shrunk  # nothing is un-merged
