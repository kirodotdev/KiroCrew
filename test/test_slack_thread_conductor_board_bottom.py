"""A Conductor thread's board stays after the latest reply.

A thread's board is not pinned, so replies bury it. When the board changes and
the thread has a newer reply under it, the board is posted again at the bottom
and the old message deleted; with nothing under it, it is edited in place. The
publisher runs against the recorded fake Slack of the thread-board tests.
"""

from __future__ import annotations

import pytest
from test_slack_thread_conductor_board import (  # noqa: F401  (fixtures)
    ANCHOR,
    CHANNEL,
    _enabled,
    _Slack,
    _World,
)

from kiro_crew.slack.code_channels import board as board_pkg
from kiro_crew.slack.code_channels import comments, surface, surface_store
from kiro_crew.slack.code_channels.board import publisher
from kiro_crew.slack.code_channels.store import thread_key

ROOM = thread_key(CHANNEL, ANCHOR)
#: The fake numbers every message in order: the first board, a reply, the moved board.
B1, B2 = "1700000101.000100", "1700000103.000100"


class _ThreadSlack(_Slack):
    """The thread fake, plus the replies read and the delete a move makes."""

    def __init__(self) -> None:
        super().__init__()
        #: The ts of every reply in the thread after the anchor.
        self.replies: list[str] = []
        self._seq = 1700000100  # after the anchor, as Slack numbers them

    def _next(self) -> str:
        self._seq += 1
        return f"{self._seq}.000100"

    async def post_blocks(self, channel, blocks, text, thread_ts=None):
        self._maybe_fail("post_blocks")
        self.calls.append(("post_blocks", channel, thread_ts, blocks, text))
        ts = self._next()
        self.replies.append(ts)
        return ts

    async def fetch_thread_replies(
        self, channel, thread_ts, limit=200, warn_on_pagination=True, *, oldest=None, latest=None
    ):
        self.calls.append(("fetch_thread_replies", channel, thread_ts, oldest))
        after = comments._ts_key(str(oldest or ""))
        newer = [ts for ts in self.replies if comments._ts_key(ts) > after]
        return [{"ts": thread_ts}] + [{"ts": ts} for ts in newer][: limit - 1]

    async def delete_message(self, channel, ts):
        self.calls.append(("delete_message", channel, ts))
        self._maybe_fail("delete_message")
        self.replies.remove(ts)

    def reply(self) -> str:
        """Someone (the conductor, a worker, the owner) replies in the thread."""
        ts = self._next()
        self.replies.append(ts)
        return ts


@pytest.fixture
def slack(_floor_monkeypatch):
    fake = _ThreadSlack()
    _floor_monkeypatch.setattr(surface, "slack", lambda: fake)

    async def _true():
        return True

    _floor_monkeypatch.setattr(surface, "outbound_permitted", _true)
    _floor_monkeypatch.setattr(surface, "audit", lambda *a, **k: None)
    return fake


@pytest.fixture
def world(tmp_path, monkeypatch, slack):
    return _World(tmp_path, monkeypatch, slack)


def _board_ts() -> str:
    record = surface_store.store().get(ROOM)
    return record.board_ts if record else ""


def _change(world, n: int) -> None:
    world.items[0].title = f"Fix the login, step {n}"


def test_a_change_under_a_newer_reply_moves_the_board_to_the_bottom(world, slack):
    world.worker("it_00000001")
    world.refresh()
    assert _board_ts() == B1
    slack.reply()
    _change(world, 2)
    world.refresh()

    posts = slack.of("post_blocks")
    assert [(c[1], c[2]) for c in posts] == [(CHANNEL, ANCHOR), (CHANNEL, ANCHOR)]
    assert slack.of("delete_message") == [("delete_message", CHANNEL, B1)]
    assert slack.of("update_message") == []
    assert _board_ts() == B2
    assert slack.replies == ["1700000102.000100", B2]  # exactly one board, after the reply
    assert "step 2" in str(posts[1][3])


def test_a_board_that_is_already_last_is_edited_in_place(world, slack):
    world.worker("it_00000001")
    world.refresh()
    _change(world, 2)
    world.refresh()

    assert len(slack.of("post_blocks")) == 1
    assert [c[2] for c in slack.of("update_message")] == [B1]
    assert slack.of("delete_message") == []
    assert _board_ts() == B1


def test_an_unchanged_board_is_not_moved_by_a_newer_reply(world, slack):
    world.worker("it_00000001")
    world.refresh()
    slack.reply()
    world.refresh()

    assert len(slack.of("post_blocks")) == 1
    assert slack.of("delete_message") == [] and slack.of("fetch_thread_replies") == []


def test_a_thread_with_no_board_yet_gets_one_posted_without_a_replies_read(world, slack):
    slack.reply()
    world.worker("it_00000001")
    world.refresh()

    assert len(slack.of("post_blocks")) == 1
    assert slack.of("fetch_thread_replies") == [] and slack.of("delete_message") == []


def test_an_old_board_already_gone_counts_as_deleted(world, slack):
    world.worker("it_00000001")
    world.refresh()
    slack.reply()
    slack.fail["delete_message"] = "message_not_found"
    _change(world, 2)
    world.refresh()
    assert slack.of("update_message") == []  # no pointer for a board already gone

    slack.replies.remove(B1)
    slack.reply()
    _change(world, 3)
    world.refresh()
    assert len(slack.of("post_blocks")) == 3  # still moves
    assert ROOM not in publisher._move_refused


def test_a_refused_delete_leaves_a_pointer_and_edits_in_place_from_then_on(world, slack):
    world.worker("it_00000001")
    world.refresh()
    slack.reply()
    slack.fail["delete_message"] = "missing_scope"
    _change(world, 2)
    world.refresh()

    assert _board_ts() == B2
    edits = slack.of("update_message")
    assert [c[2] for c in edits] == [B1]  # the old board becomes a pointer
    assert edits[0][4] == publisher.MOVED_TEXT
    assert ROOM in publisher._move_refused

    slack.reply()
    _change(world, 3)
    world.refresh()
    assert len(slack.of("post_blocks")) == 2  # no third board
    assert [c[2] for c in slack.of("update_message")] == [B1, B2]


def test_a_transient_delete_and_failed_pointer_stop_further_moves(world, slack):
    world.worker("it_00000001")
    world.refresh()
    slack.reply()
    slack.fail["delete_message"] = "ratelimited"
    slack.fail["update_message"] = "ratelimited"
    _change(world, 2)
    world.refresh()

    assert _board_ts() == B2
    assert slack.of("delete_message") == [("delete_message", CHANNEL, B1)]
    assert [c[2] for c in slack.of("update_message")] == [B1]
    assert slack.of("update_message")[0][4] == publisher.MOVED_TEXT
    assert ROOM in publisher._move_refused

    slack.reply()
    _change(world, 3)
    world.refresh()
    assert len(slack.of("post_blocks")) == 2
    assert [c[2] for c in slack.of("update_message")] == [B1, B2]
    assert "step 3" in str(slack.of("update_message")[-1][3])
    assert _board_ts() == B2


def test_a_failed_replies_read_edits_in_place(world, slack, monkeypatch):
    world.worker("it_00000001")
    world.refresh()
    slack.reply()

    async def _boom(*a, **k):
        raise RuntimeError("history not readable")

    monkeypatch.setattr(slack, "fetch_thread_replies", _boom)
    _change(world, 2)
    world.refresh()
    assert len(slack.of("post_blocks")) == 1
    assert [c[2] for c in slack.of("update_message")] == [B1]


def test_many_rapid_changes_move_the_board_once_per_edit_gap(world, slack, monkeypatch):
    now = [5000.0]
    monkeypatch.setattr(publisher, "_clock", lambda: now[0])
    monkeypatch.setattr(publisher, "_page_time", lambda: now[0])
    world.worker("it_00000001")
    world.refresh()
    for n in range(2, 12):
        slack.reply()
        _change(world, n)
        now[0] += 1.0  # ten changes inside one MIN_EDIT_GAP_SECS
        world.refresh()
    assert len(slack.of("post_blocks")) == 1  # all held back by the gap
    assert slack.of("delete_message") == []

    now[0] += publisher.MIN_EDIT_GAP_SECS
    world.refresh()
    posts = slack.of("post_blocks")
    assert len(posts) == 2 and "step 11" in str(posts[1][3])  # the latest state, once
    assert slack.of("delete_message") == [("delete_message", CHANNEL, B1)]
    assert slack.replies[-1] == _board_ts() == "1700000112.000100"


def test_after_a_restart_the_stored_board_is_the_one_moved_and_deleted(world, slack, tmp_path):
    world.worker("it_00000001")
    world.refresh()
    # A gateway restart: in-process state gone, the surface store read back from disk.
    board_pkg.reset_for_tests()
    surface_store.set_store(surface_store.SurfaceStore(tmp_path / "surface.json"))
    assert _board_ts() == B1

    slack.reply()
    _change(world, 2)
    world.refresh()
    assert slack.of("delete_message") == [("delete_message", CHANNEL, B1)]
    assert _board_ts() == B2
    assert len(slack.of("post_blocks")) == 2


def test_ts_order_is_exact_past_a_floats_digits():
    assert comments._ts_key("1700000000.000101") > comments._ts_key("1700000000.000100")
    assert comments._ts_key("1700000000.1") == comments._ts_key("1700000000.100000")
    assert comments._ts_key("") == (0, 0) and comments._ts_key("x.y") == (0, 0)
