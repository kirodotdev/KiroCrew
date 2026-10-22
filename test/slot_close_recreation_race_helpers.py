"""Fakes, seams and slot builders shared by the slot-close recreation race tests.

``test_slot_close_recreation_race.py`` and its ``_privacy_ratchet`` and
``_handover_metadata`` siblings import these by name. ``_no_nudge_service`` is autouse,
so every one of them closes with no auto-nudge service to retire. ``_Req`` is the fake
request the close handlers read, ``_reached`` parks a test at a teardown await, and the
slot builders seed the shapes the hand-over drains. Where the ``_Req`` comment and
docstring and the ``_reached`` docstring say "here", "this file" or "below" about callers
and tests, they mean the ``test_slot_close_recreation_race*.py`` modules that import them.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew import autonudge
from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.testing.wait import await_until

NAME = "chat-1-1785"


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_slot_close_recreation_race*.py modules calls monkeypatch.undo()
def _no_nudge_service(monkeypatch):
    """No auto-nudge service: these tests isolate the teardown-race guard.

    The nudge-loop retirement and its rollbacks are pinned by
    test_slot_close_nudge_race.py; here the close path must find nothing to
    retire so the only variable is the post-pop identity re-check.
    """
    monkeypatch.setattr(autonudge, "_INSTANCE", None)


class _Stream:
    """The ``request.content`` half of the fake: a one-shot chunked reader.

    ``read_bounded_json`` reads the body off the stream rather than calling
    ``request.json()`` whenever a byte cap is in force, so a double that only
    stubs ``json`` reads an EMPTY body on the capped path. Chunking at the
    helper's own 8 KiB step keeps the fake honest for a body large enough to
    arrive in several iterations.
    """

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._raw), n):
            yield self._raw[i : i + n]


class _Req:
    """Minimal stand-in for the aiohttp request the handlers read.

    The race tests drive the handlers directly rather than through a client: the
    interleaving of the concurrent recreate has to be scheduled deterministically
    inside the teardown window, and a client's own awaits would let it run before
    the handler even reached the pop.

    The body surface mirrors what ``read_bounded_json`` actually touches --
    ``can_read_body`` first, then ``content_length``/``content``/``charset`` on the
    capped path -- not just ``json()``. ``api_chat_slots_cleanup`` moved onto that
    helper, and a double missing ``can_read_body`` does not merely fail: the
    handler raises before it reaches the seam the race tests park on, so the
    ``entered`` event never fires and every interleaved test reports the park
    rather than the attribute error underneath it. ``_reached`` is what keeps that
    shape a named failure instead of a run-length timeout.
    """

    def __init__(self, state, slot: str = NAME, body: dict | None = None) -> None:
        self.app = {"state": state}
        self.match_info = {"slot": slot}
        self._has_body = body is not None
        self._body = body if body is not None else {}
        # ``body is None`` is NO body, which is what every call site here sends and
        # what ``can_read_body`` must read as False. An explicitly-passed ``{}`` is a
        # body that is PRESENT and empty, so keying off truthiness instead would make
        # the double answer False for a request aiohttp reports as readable.
        self._raw = b"" if body is None else json.dumps(self._body).encode()
        self.charset = "utf-8"
        self.content = _Stream(self._raw)

    @property
    def content_length(self) -> int | None:
        return len(self._raw) or None

    def get(self, key: str, default: str = "") -> str:
        del key
        return default

    @property
    def can_read_body(self) -> bool:
        """Mirror ``aiohttp.web.BaseRequest.can_read_body``: unread body pending.

        Every caller in this file omits ``body``, so the double must report the
        same "nothing to read" outcome the real request would for a bodyless
        request — that is what lets ``read_bounded_json(..., allow_absent=True)``
        take its empty-object path instead of raising on a stand-in that never
        modeled this property.
        """
        return self._has_body

    async def json(self) -> dict:
        return self._body


def _arm_running_turn(slot, entered: asyncio.Event, release: asyncio.Event):
    """Give ``slot`` a live, parkable turn so its task-cancel await blocks.

    The close pops the slot and then, if ``slot.running``, cancels ``slot.task``
    and waits on ``asyncio.wait_for(asyncio.shield(slot.task), 2.0)``. To land a
    recreate in THAT await (the window the first pre-save guard protects) the
    task has to still be pending when the recreate is minted. This turn swallows
    the handler's cancel, signals ``entered`` so the test can mint the
    replacement, then parks on ``release`` before returning — so the handler's
    shielded wait is still blocked at the exact instant the key changes owner.
    ``slot.running`` is ``self.task is not None and not self.task.done()``, so
    assigning this task is all it takes to make the slot look busy.
    """

    async def _turn() -> None:
        try:
            # flake-ok: the fake turn parks here until the close cancels it; moved verbatim
            await asyncio.Event().wait()  # block until cancelled
        except asyncio.CancelledError:
            entered.set()
            # flake-ok: the fake turn parks here until the test sets release; moved verbatim
            await release.wait()

    slot.task = asyncio.create_task(_turn())
    return slot.task


async def _reached(
    entered: asyncio.Event,
    close: asyncio.Task[object],
    *,
    seam: str = "save_slot_off_loop",
) -> None:
    """Wait for the close to reach its interleave seam, BOUNDED so a miss names itself.

    Every interleaving test below opens the recreate's window by parking on an
    event only the close's own progress can set — the monkeypatched
    ``save_slot_off_loop``, or the cancelled turn ``_arm_running_turn`` armed. The
    whole premise is that the close REACHES that seam, and a close that returns,
    raises or blocks short of it never sets the event: a teardown that stops
    cancelling the live turn (``slot.running`` read through a renamed field, say)
    leaves every ``_arm_running_turn`` case unarmed, and a preamble that raises or
    answers 404 before the pop — the ``can_read_body`` shape ``_Req`` documents —
    leaves every ``_persist`` case unentered.

    Unbounded, such a regression does not FAIL these tests, it PARKS them until the
    repo-wide ``--timeout``: on Linux and macOS a Timeout traceback and minutes of
    shard time per test instead of the one line below, and on Windows — no SIGALRM,
    so pytest-timeout kills the xdist worker, and CI runs
    ``--max-worker-restart=0`` — an aborted run whose unreached results do not
    exist at all (testing-conventions flake class 6). 5.0s is orders of magnitude
    more than the single loop hop this needs and far under that ceiling, so the
    deadline stays on the await whose property is under test and the failure is
    attributed to this test rather than to the run.

    Only the TEST side is bounded. The ``release.wait()`` calls inside the
    ``_persist`` stubs and ``_arm_running_turn`` run on the ``close`` task, which
    pytest tears down with the loop; bounding those would change what the close
    parks on, which is the interleave itself. The pending ``close`` is cancelled on
    the timeout path so the named assertion is not followed by a task destroyed at
    loop teardown and blamed on whichever test runs next.
    """
    try:
        await await_until(entered, timeout=5.0, what=f"the close reaching {seam}")
    except AssertionError:
        close.cancel()
        raise AssertionError(
            f"the close never reached {seam}: it returned or blocked short of the "
            "interleave window, so the recreate could never be minted inside it"
        ) from None


def _make_stale(state, name: str = NAME):
    """Age the slot's last activity past the 3-day cleanup cutoff."""
    slot = state._slots[name]
    slot.created_at = "2000-01-01T00:00:00+00:00"
    for m in slot.messages:
        m["ts"] = "2000-01-01T00:00:00+00:00"
    return slot


RKEY = f"dashboard:{NAME}"


def _json(resp) -> dict:
    """Decode an aiohttp json_response body to a dict."""
    import json

    return json.loads(resp.body)


HKEY = f"dashboard:{NAME}"


def _disk_contents(state) -> list[str]:
    """The transcript's message contents, in file order, read back from disk."""
    return [m.get("content", "") for m in state.conversation_log.read_messages(HKEY)]


async def _slot_with_committed_and_uncommitted_rows(state, name: str = NAME):
    """A slot with two rows on disk and two rows that have never been written.

    ``_disk_window_len`` is what the last committed save covered, so this is the
    shape that makes "the tail was lost" observable: a hand-over that writes
    nothing leaves the file holding only the first two.
    """
    slot = state.get_or_create_slot(name)
    slot.append("user", "PERSISTED-1")
    slot.append("assistant", "PERSISTED-2")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False)
    assert slot._disk_window_len == 2, "the seed save did not commit the first window"
    slot.append("user", "TAIL-3")
    slot.append("assistant", "TAIL-4")
    slot.drain()
    return slot


async def _publish_metadata(
    state, slot, *, title: str, folder: str, origin: str = "auto", refresh_mark: int = 0
) -> None:
    """Commit ``slot``'s title and folder the way every metadata route does.

    A forced save is what the tag / folder / pin / recreate-PATCH routes use, and
    for a message-less slot it is the empty-window ``update_metadata_if`` merge —
    the exact shape a replacement born from ``POST /api/chat/slots`` with a folder
    or a pinned title takes.

    ``origin`` sets the title's provenance the way the titling paths do — ``"auto"``
    for a generated name, ``"user"`` for a manual rename — and ``refresh_mark`` the
    background-refresh budget already spent, because the save persists BOTH
    alongside the title. A pairing test that left these at their defaults would read
    a consistent line no matter which slot each half came from.
    """
    slot.title = title
    slot.folder_id = folder
    slot._titled = True
    slot._title_origin = origin
    slot._title_refresh_mark = refresh_mark
    assert await handlers.save_slot_off_loop(state, slot, force=True, best_effort=False)
