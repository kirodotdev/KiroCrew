"""Channel switching and the standalone gateway restart.

Two capabilities that complete the non-desktop update flow:

* ``POST /api/update/channel`` moves a feed-checkable install onto another
  release lane. The channel name becomes a path segment in every feed URL and an
  argument in the recommended installer command, so the allowlist is the load-
  bearing guard and is asserted here directly.
* ``POST /api/restart`` restarts the gateway WITHOUT updating. Before it existed,
  a wheel install that had just run the copied installer command in a terminal
  was still executing the old code with no in-app way to reload.
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform import update_layout
from kiro_crew.platform.update_layout import InstallLayout


@pytest.fixture(autouse=True)
def _isolated_channel_home(monkeypatch, tmp_path):
    """Point the channel file at a tmp dir so no test touches the real data home."""
    monkeypatch.setattr(update_layout, "data_home", lambda: tmp_path)
    return tmp_path


def _request(body: object) -> web.Request:
    """A minimal request stub: only ``.json()`` and ``app["state"]`` are read."""
    req = MagicMock()

    async def _json() -> object:
        if isinstance(body, Exception):
            raise body
        return body

    req.json = _json
    state = MagicMock()
    state._gateway_restart_task = None
    state._gateway_restart_in_progress = False
    req.app = {"state": state}
    return req


class TestSetReleaseChannel:
    """The writer that owns the channel file."""

    def test_round_trips_every_published_channel(self, _isolated_channel_home):
        for channel in update_layout.RELEASE_CHANNELS:
            assert update_layout.set_release_channel(channel) == channel
            assert update_layout.release_channel() == channel

    def test_normalizes_case_and_whitespace(self, _isolated_channel_home):
        assert update_layout.set_release_channel("  Insider \n") == "insider"
        assert update_layout.release_channel() == "insider"

    def test_writes_the_same_byte_format_cli_sh_writes(self, _isolated_channel_home):
        # cli.sh does `printf '%s\n' "$CHANNEL"`. Matching it keeps the two
        # writers interchangeable; a missing newline would still read back fine
        # but would make the files differ for no reason.
        update_layout.set_release_channel("nightly")
        assert (_isolated_channel_home / "channel").read_text(encoding="utf-8") == "nightly\n"

    @pytest.mark.parametrize(
        "junk",
        [
            "",
            "   ",
            "beta",
            "../../etc/passwd",
            "stable/../nightly",
            "stable\nnightly",
            "https://evil.example/feed",
        ],
    )
    def test_rejects_anything_off_the_allowlist(self, junk, _isolated_channel_home):
        # REJECT, never sanitize: the value lands in a feed URL path segment and
        # in a shell command. A traversal or newline that merely got stripped
        # would still prove the guard is a filter rather than a gate.
        with pytest.raises(ValueError):
            update_layout.set_release_channel(junk)
        assert not (_isolated_channel_home / "channel").exists()

    def test_a_rejected_write_leaves_a_prior_channel_intact(self, _isolated_channel_home):
        update_layout.set_release_channel("insider")
        with pytest.raises(ValueError):
            update_layout.set_release_channel("beta")
        assert update_layout.release_channel() == "insider"

    def test_leaves_no_temp_file_behind(self, _isolated_channel_home):
        update_layout.set_release_channel("stable")
        assert [p.name for p in _isolated_channel_home.iterdir()] == ["channel"]


class TestChannelEndpoint:
    """``POST /api/update/channel``."""

    def _feed_layout(self) -> InstallLayout:
        return InstallLayout(
            kind="wheel", proj="", is_git=False, is_externally_managed=False, guidance=""
        )

    def test_switches_and_rechecks_against_the_new_feed(self, _isolated_channel_home):
        update_layout.set_release_channel("stable")
        checked: list[None] = []

        async def _fake_check() -> None:
            checked.append(None)

        with (
            patch.object(updates, "detect_install_layout", return_value=self._feed_layout()),
            patch.object(updates, "_do_update_check", _fake_check),
        ):
            resp = asyncio.run(updates.api_update_channel(_request({"channel": "insider"})))

        assert resp.status == 200
        assert update_layout.release_channel() == "insider"
        # The re-check is what stops the panel from presenting the PREVIOUS
        # lane's verdict as this lane's answer.
        assert len(checked) == 1

    def test_the_switch_never_reads_the_channel_file_on_the_event_loop(
        self, _isolated_channel_home
    ):
        """The invalidation must not re-read ``$KIROCREW_HOME/channel``.

        The endpoint is a coroutine, and ``release_channel()`` is a synchronous
        ``read_text`` on the data home — which the operator may have put on NFS or
        SMB, where a read can stall long enough to freeze the event loop and the
        liveness heartbeat with it. The validated channel is already in hand from
        the write, so any read here is both a stall risk and redundant.
        """
        update_layout.set_release_channel("stable")

        def _explode() -> str:  # pragma: no cover - must not be called
            raise AssertionError("release_channel() must not be read on the event loop")

        async def _fake_check() -> None:
            return None

        with (
            patch.object(updates, "detect_install_layout", return_value=self._feed_layout()),
            patch.object(updates, "_do_update_check", _fake_check),
            patch.object(updates, "_release_channel", _explode),
        ):
            resp = asyncio.run(updates.api_update_channel(_request({"channel": "insider"})))

        assert resp.status == 200
        # The channel the user just chose still has to reach the client: carrying
        # it through the reset is what keeps the switcher from blanking.
        assert json.loads(resp.text)["channel"] == "insider"

    def test_the_superseded_branch_never_reads_the_channel_on_the_loop(
        self, _isolated_channel_home
    ):
        """The discard path must offload its channel read, not block the loop.

        Same hazard as the invalidation: ``release_channel()`` is a synchronous
        read of the data home, which can be an NFS/SMB mount. Here the value cannot
        simply be passed in — the file is the authority on which lane the install
        now follows — so it has to move off the loop instead.
        """
        update_layout.set_release_channel("stable")
        reads: list[str] = []

        def _tracked_read() -> str:
            # Records the CALLING thread: the whole point is that this does not run
            # on the loop's thread.
            reads.append(threading.current_thread().name)
            return "nightly"

        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_feed_check(*_a, **_k):
            started.set()
            await release.wait()
            return {"update_available": True, "latest_version": "9.9.9"}

        async def _scenario() -> None:
            loop_thread = threading.current_thread().name
            with (
                patch.object(updates, "detect_install_layout", return_value=self._feed_layout()),
                patch.object(updates, "_check_release_feed", _slow_feed_check),
                patch.object(updates, "_release_channel", _tracked_read),
            ):
                slow = asyncio.create_task(updates._do_update_check())
                await started.wait()
                updates._invalidate_update_check("nightly")
                release.set()
                await slow

            assert reads, "the discard path must still resolve the current channel"
            assert (
                loop_thread not in reads
            ), f"the channel read ran on the event loop thread: {reads}"

        asyncio.run(_scenario())

    def test_the_guard_is_held_across_the_awaited_cleanup(self, _isolated_channel_home):
        """A newer verdict must not be erased by the check it superseded.

        The discard path awaits a channel read. If the single-flight guard were
        released BEFORE that await, a status poll could start and finish a fresh
        check while this one is parked, and the reset would then land on top of the
        newer answer.
        """
        update_layout.set_release_channel("stable")
        seen: list[bool] = []

        def _slow_read() -> str:
            # Sampled from the worker thread while the coroutine is parked on it:
            # this is exactly the window a second check could squeeze into.
            seen.append(updates._check_in_flight)
            return "nightly"

        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_feed_check(*_a, **_k):
            started.set()
            await release.wait()
            return {"update_available": True, "latest_version": "9.9.9"}

        async def _scenario() -> None:
            with (
                patch.object(updates, "detect_install_layout", return_value=self._feed_layout()),
                patch.object(updates, "_check_release_feed", _slow_feed_check),
                patch.object(updates, "_release_channel", _slow_read),
            ):
                slow = asyncio.create_task(updates._do_update_check())
                await started.wait()
                updates._invalidate_update_check("nightly")
                release.set()
                await slow

        asyncio.run(_scenario())
        assert seen == [True], (
            "the single-flight guard was already released during the awaited "
            f"cleanup, so a concurrent check could interleave: {seen}"
        )
        # And it must not stay held afterwards — that leak stops every future check.
        assert updates._check_in_flight is False

    def test_a_failure_in_the_cleanup_still_releases_the_guard(self, _isolated_channel_home):
        """The inverse hazard, and the worse one.

        A raise inside the cleanup must not leave `_check_in_flight` stuck True:
        that silently stops the updater checking for the life of the process.
        """
        update_layout.set_release_channel("stable")

        def _explode() -> str:
            raise OSError("channel file unreadable")

        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_feed_check(*_a, **_k):
            started.set()
            await release.wait()
            return {"update_available": False}

        async def _scenario() -> None:
            with (
                patch.object(updates, "detect_install_layout", return_value=self._feed_layout()),
                patch.object(updates, "_check_release_feed", _slow_feed_check),
                patch.object(updates, "_release_channel", _explode),
            ):
                slow = asyncio.create_task(updates._do_update_check())
                await started.wait()
                updates._invalidate_update_check("nightly")
                release.set()
                with pytest.raises(OSError):
                    await slow

        asyncio.run(_scenario())
        assert updates._check_in_flight is False, (
            "the guard leaked on the error path — every future update check is now "
            "a no-op and the updater has silently stopped"
        )

    def test_stale_verdict_is_dropped_even_if_the_recheck_no_ops(self, _isolated_channel_home):
        """A check already in flight makes ``_do_update_check`` return early.

        The response must then say "not checked" rather than echo the old
        channel's verdict and ``latest_version`` as though they applied here.
        """
        update_layout.set_release_channel("stable")
        updates._set_update_info(
            update_available=True, latest_version="9.9.9", check_status="succeeded"
        )

        with (
            patch.object(updates, "detect_install_layout", return_value=self._feed_layout()),
            patch.object(updates, "_check_in_flight", True),
        ):
            resp = asyncio.run(updates.api_update_channel(_request({"channel": "nightly"})))

        assert resp.status == 200
        assert updates._update_info["check_status"] == "unchecked"
        assert updates._update_info["update_available"] is None
        assert updates._update_info["latest_version"] == ""
        # The switcher reads `channel` off this response. The invalidated cache
        # holds "" for it, so the stored value must win or a successful switch
        # blanks the control that just performed it.
        payload = json.loads(resp.body.decode())
        assert payload["channel"] == "nightly"
        # And the command must name the NEW lane. Left empty, the client falls back
        # to the command shipped in status -- the PREVIOUS channel's -- so copying
        # it would move the install straight back.
        assert "--channel nightly" in payload["update_command"]

    @pytest.mark.parametrize("junk", ["beta", "../../etc/passwd", ""])
    def test_rejects_an_unknown_channel_without_writing(self, junk, _isolated_channel_home):
        with patch.object(updates, "detect_install_layout", return_value=self._feed_layout()):
            resp = asyncio.run(updates.api_update_channel(_request({"channel": junk})))
        assert resp.status == 400
        assert not (_isolated_channel_home / "channel").exists()

    def test_rejects_a_non_string_channel(self, _isolated_channel_home):
        with patch.object(updates, "detect_install_layout", return_value=self._feed_layout()):
            resp = asyncio.run(updates.api_update_channel(_request({"channel": ["insider"]})))
        assert resp.status == 400

    def test_rejects_invalid_json(self, _isolated_channel_home):
        resp = asyncio.run(updates.api_update_channel(_request(ValueError("bad json"))))
        assert resp.status == 400

    @pytest.mark.parametrize("body", [[], "insider", 7, None, True])
    def test_rejects_a_non_object_body_with_400_not_500(self, body, _isolated_channel_home):
        """A JSON array or scalar parses fine and then has no ``.get``.

        Without an explicit type check the handler raises AttributeError and
        answers 500 to an authenticated caller, where 400 is the honest answer.
        """
        with patch.object(updates, "detect_install_layout", return_value=self._feed_layout()):
            resp = asyncio.run(updates.api_update_channel(_request(body)))
        assert resp.status == 400
        assert not (_isolated_channel_home / "channel").exists()

    def test_a_check_superseded_by_a_switch_cannot_write_its_verdict(self, _isolated_channel_home):
        """An in-flight check against the OLD feed must not land after the switch.

        The in-flight guard cannot cancel a running check, so a check that started
        on the previous channel would otherwise finish afterwards, write that
        lane's verdict into the cache and stamp the 12-hourly clock -- pinning a
        stale answer for half a day to a channel this install no longer follows.
        """
        update_layout.set_release_channel("stable")

        async def _scenario() -> None:
            started = asyncio.Event()
            release = asyncio.Event()

            async def _slow_feed_check(capability: object) -> None:
                started.set()
                await release.wait()
                # The verdict the OLD channel's feed would have produced.
                updates._set_update_info(
                    managed_by="kirocrew",
                    channel="stable",
                    update_available=True,
                    latest_version="1.2.3",
                    check_status="succeeded",
                )

            with (
                patch.object(updates, "detect_install_layout", return_value=self._feed_layout()),
                patch.object(updates, "_check_release_feed", _slow_feed_check),
            ):
                slow = asyncio.create_task(updates._do_update_check())
                await started.wait()
                # Switch channels while that check is still talking to the old feed.
                updates._invalidate_update_check("nightly")
                update_layout.set_release_channel("nightly")
                release.set()
                await slow

        asyncio.run(_scenario())

        # The superseded verdict was discarded, not published.
        assert updates._update_info["check_status"] == "unchecked"
        assert updates._update_info["update_available"] is None
        assert updates._update_info["latest_version"] == ""
        # And the clock stays unstamped so the next poll re-checks the NEW lane
        # immediately instead of waiting out the 12-hour interval.
        assert updates._last_update_check == 0.0

    def test_refuses_a_git_checkout(self, _isolated_channel_home):
        # A git checkout follows its remote; writing a channel file would be a
        # control that appears to work and changes nothing.
        layout = InstallLayout(
            kind="git", proj="/x", is_git=True, is_externally_managed=False, guidance=""
        )
        with patch.object(updates, "detect_install_layout", return_value=layout):
            resp = asyncio.run(updates.api_update_channel(_request({"channel": "insider"})))
        assert resp.status == 409
        assert not (_isolated_channel_home / "channel").exists()

    @pytest.mark.parametrize("kind", ["dmg", "appimage", "docker"])
    def test_refuses_an_externally_managed_install(self, kind, _isolated_channel_home):
        layout = InstallLayout(
            kind=kind,
            proj="",
            is_git=False,
            is_externally_managed=True,
            guidance=update_layout.EXTERNALLY_MANAGED[kind],
        )
        with patch.object(updates, "detect_install_layout", return_value=layout):
            resp = asyncio.run(updates.api_update_channel(_request({"channel": "insider"})))
        assert resp.status == 409
        assert not (_isolated_channel_home / "channel").exists()

    def test_refuses_a_command_managed_install(self, _isolated_channel_home):
        """A policy-pinned provider never reads the channel file — refuse, don't lie.

        The layout probe is booby-trapped: the refusal must fire BEFORE the git
        shell-out, because a command-managed host owes the caller the same
        answer regardless of what the install tree happens to look like.
        """
        from kiro_crew.platform.update_provider import CommandProvider

        provider = CommandProvider(check_command="check-cmd")
        with (
            patch.object(updates, "resolve_provider", return_value=provider),
            patch.object(
                updates,
                "detect_install_layout",
                side_effect=AssertionError("layout probe must not run"),
            ),
        ):
            resp = asyncio.run(updates.api_update_channel(_request({"channel": "insider"})))
        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "channel_not_applicable_command_managed"
        assert not (_isolated_channel_home / "channel").exists()

    def test_reports_a_write_failure_instead_of_claiming_success(self, _isolated_channel_home):
        with (
            patch.object(updates, "detect_install_layout", return_value=self._feed_layout()),
            patch.object(updates, "set_release_channel", side_effect=OSError("read-only fs")),
        ):
            resp = asyncio.run(updates.api_update_channel(_request({"channel": "insider"})))
        assert resp.status == 500


class TestRestartEndpoint:
    """``POST /api/restart``."""

    def test_replies_before_restarting(self):
        """The response must be produced without awaiting the restart.

        ``os.execv`` replaces the process image, so a restart performed inline
        would tear down the connection mid-response and the client could not tell
        "restarting" from "the request failed".
        """
        started = asyncio.Event()

        async def _fake_restart(_state: object) -> None:
            started.set()

        async def _run() -> web.Response:
            req = _request({})
            req.app["state"]._background_tasks = set()
            with patch.object(updates, "_restart_gateway", _fake_restart):
                resp = await updates.api_gateway_restart(req)
                # Not yet restarted when the response is handed back.
                assert not started.is_set()
                await asyncio.sleep(0.4)
                assert started.is_set()
            return resp

        resp = asyncio.run(_run())
        assert resp.status == 200

    def test_a_restart_failure_is_surfaced_not_swallowed(self):
        async def _boom(_state: object) -> None:
            raise RuntimeError("exec failed")

        async def _run() -> MagicMock:
            req = _request({})
            req.app["state"]._background_tasks = set()
            with patch.object(updates, "_restart_gateway", _boom):
                await updates.api_gateway_restart(req)
                await asyncio.sleep(0.4)
            return req.app["state"]

        state = asyncio.run(_run())
        # The user is told, rather than left watching a spinner that never ends.
        assert state.push_update_progress.called

    def test_duplicate_requests_share_one_restart_task(self):
        """Double-clicks cannot race two successors for one gateway port."""
        calls = 0
        release = asyncio.Event()

        async def _fake_restart(_state: object) -> None:
            nonlocal calls
            calls += 1
            await release.wait()

        async def _run() -> tuple[web.Response, web.Response]:
            req = _request({})
            req.app["state"]._background_tasks = set()
            with patch.object(updates, "_restart_gateway", _fake_restart):
                first = await updates.api_gateway_restart(req)
                second = await updates.api_gateway_restart(req)
                assert json.loads(second.body)["already_in_progress"] is True
                await asyncio.sleep(0.3)
                assert calls == 1
                release.set()
                await asyncio.sleep(0)
            return first, second

        first, second = asyncio.run(_run())
        assert first.status == 200
        assert second.status == 200
