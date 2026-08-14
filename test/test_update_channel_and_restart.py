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
    req.app = {"state": MagicMock()}
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

    def test_stale_verdict_is_dropped_even_if_the_recheck_no_ops(self, _isolated_channel_home):
        """A check already in flight makes ``_do_update_check`` return early.

        The response must then say "not checked" rather than echo the old
        channel's ``available``/``remote_version`` as though they applied here.
        """
        update_layout.set_release_channel("stable")
        updates._set_update_info(available=True, remote_version="9.9.9", checked=True)

        with (
            patch.object(updates, "detect_install_layout", return_value=self._feed_layout()),
            patch.object(updates, "_check_in_flight", True),
        ):
            resp = asyncio.run(updates.api_update_channel(_request({"channel": "nightly"})))

        assert resp.status == 200
        assert updates._update_info["checked"] is False
        assert updates._update_info["available"] is False
        assert updates._update_info["remote_version"] == ""
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

    def test_a_check_superseded_by_a_switch_cannot_write_its_verdict(
        self, _isolated_channel_home
    ):
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

            async def _slow_feed_check(install_kind: str) -> None:
                started.set()
                await release.wait()
                # The verdict the OLD channel's feed would have produced.
                updates._set_update_info(
                    install_kind=install_kind,
                    channel="stable",
                    available=True,
                    remote_version="1.2.3",
                    checked=True,
                )

            with (
                patch.object(updates, "detect_install_layout", return_value=self._feed_layout()),
                patch.object(updates, "_check_release_feed", _slow_feed_check),
            ):
                slow = asyncio.create_task(updates._do_update_check())
                await started.wait()
                # Switch channels while that check is still talking to the old feed.
                updates._invalidate_update_check()
                update_layout.set_release_channel("nightly")
                release.set()
                await slow

        asyncio.run(_scenario())

        # The superseded verdict was discarded, not published.
        assert updates._update_info["checked"] is False
        assert updates._update_info["available"] is False
        assert updates._update_info["remote_version"] == ""
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
