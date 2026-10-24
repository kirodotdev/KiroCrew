"""close()-time thread-status clearing for SlackRenderer.

Split out of ``test_slack_renderer.py`` to keep that file under the repo's
2,000-line cap (see AGENTS.md). Reuses the recording Slack client and the
renderer imports from the parent module, so the fixtures stay in one place.

A cancelled turn (``!stop``, a warm-pool reclaim, shutdown) makes
``TurnDriver.run()`` skip ``on_done``; the dispatcher's ``finally`` then calls
``close()``. ``on_turn_start`` has already set the native "is working" thread
status, and only ``on_done`` cleared it -- so ``close()`` must clear it too,
best-effort, or the Slack thread is left showing "...is working" for a turn
that already ended.
"""

from __future__ import annotations

import asyncio

from test_slack_renderer import _RecSlack

from kiro_crew.slack.renderer import _STATUS_WORKING, SlackRenderer


class TestRendererCloseClearsStuckStatus:
    @staticmethod
    def _statuses(rec):
        return [kw["status"] for (m, kw) in rec.calls if m == "set_thread_status"]

    def test_close_clears_working_status_on_cancelled_turn(self):
        async def scenario():
            rec = _RecSlack()
            renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
            await renderer.on_turn_start()  # sets the "is working" status
            assert self._statuses(rec) == [_STATUS_WORKING]
            # Turn cancelled: on_done never fires; dispatcher finally -> close().
            await renderer.close()
            # The working status must be cleared, not left stuck.
            statuses = self._statuses(rec)
            assert (
                statuses and statuses[-1] == ""
            ), f"close() on an unfinalized turn must clear the thread status; saw {statuses!r}"
            assert renderer._finalized is True

        asyncio.run(scenario())

    def test_close_clears_when_cancelled_after_finalize_before_clear(self):
        # on_done sets _finalized=True BEFORE it awaits the file upload and only
        # then clears the status. A cancellation inside that window leaves the
        # status set with _finalized already True. close() must still clear it
        # (its guard is _status_cleared, not _finalized), or the indicator is
        # left stuck exactly as the pre-fix bug.
        async def scenario():
            rec = _RecSlack()
            renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
            await renderer.on_turn_start()  # sets the "is working" status
            # Reproduce the on_done post-finalize / pre-clear state: finalized
            # set, status still showing, clear not yet reached.
            renderer._finalized = True
            assert renderer._status_cleared is False
            await renderer.close()
            statuses = self._statuses(rec)
            assert statuses and statuses[-1] == "", (
                "close() must clear the status even when _finalized was already "
                f"set before the clear; saw {statuses!r}"
            )

        asyncio.run(scenario())

    def test_close_does_not_clear_when_turn_never_started(self):
        # No on_turn_start -> no "is working" status was ever set, so close()
        # must not issue a spurious clear against a thread it never touched.
        async def scenario():
            rec = _RecSlack()
            renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
            await renderer.close()
            assert self._statuses(rec) == []

        asyncio.run(scenario())

    def test_close_after_on_done_does_not_double_clear(self):
        # on_done already cleared the status and finalized; close() is a no-op
        # and must not emit a second (redundant) clear.
        async def scenario():
            rec = _RecSlack()
            renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
            await renderer.on_turn_start()
            await renderer.on_done()  # clears status, marks _finalized
            cleared_after_done = self._statuses(rec).count("")
            await renderer.close()
            assert self._statuses(rec).count("") == cleared_after_done

        asyncio.run(scenario())

    def test_close_status_clear_failure_never_raises(self):
        # A raising set_thread_status during teardown must be swallowed: close()
        # is the dispatcher's finally and must never mask the cancellation.
        async def scenario():
            rec = _RecSlack()

            async def boom(channel, thread_ts, status):
                rec.calls.append(("set_thread_status", {"status": status}))
                raise RuntimeError("slack down")

            renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
            await renderer.on_turn_start()
            rec.set_thread_status = boom  # fail the teardown clear
            await renderer.close()  # must not raise
            assert renderer._finalized is True

        asyncio.run(scenario())
