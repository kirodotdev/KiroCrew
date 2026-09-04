"""Guard: a channel the running bytes never came from must not be claimed.

Promotion re-points the soaked candidate's bytes at the stable channel WITHOUT
re-stamping them, so the stable lane's current release is literally ``0.4.1rc1``.
Two consequences, and every bug in this file is one of them:

* The version string cannot answer "which lane is this install on". A rule that
  reads an ``rc`` stamp as "insider install" is wrong for the entire
  promoted-stable population, which is what made the About panel tell every
  stable user they were mid-channel-switch, forever.
* Following the stable channel does NOT make the running bytes a stable release.
  Flipping the switcher to stable while running an insider build folded
  ``0.5.0rc3`` to a clean ``v0.5.0`` -- a release that does not exist -- and put
  a green "Up to date" beside it, because the stable feed's ``0.4.1rc1`` is
  indeed not newer.

The honest input for both is the FEED's own answer, which is why the predicate
lives beside the feed check and not in a version-string heuristic.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.handlers import updates as up


@pytest.fixture
def cache(monkeypatch):
    """Fresh ``_update_info`` + a pinned local version, restored after the test."""

    def _apply(local: str, **fields: object):
        monkeypatch.setattr(up, "_local_version", local)
        up._set_update_info(**fields)
        return up._update_info

    original = dict(up._update_info)
    yield _apply
    up._update_info.clear()
    up._update_info.update(original)


def test_insider_build_following_stable_keeps_its_stamp(cache):
    # The user flipped the switcher to stable; the bytes are still 0.5.0rc3 and
    # the stable lane publishes 0.4.1rc1. Folding here would invent 0.5.0.
    cache("0.5.0rc3", channel="stable", latest_version="0.4.1rc1", channel_move_pending=True)
    assert up._display_local_version() == "0.5.0rc3"
    assert up.status_update_fields()["update_channel_move_pending"] is True
    # ...and the panel is still told what stable DOES publish, folded for display,
    # so the note can name the version the move lands on.
    assert up.status_update_fields()["update_latest_version_display"] == "0.4.1"


def test_promoted_stable_build_still_folds(cache):
    # The fold's actual population: bytes that ARE the stable release and merely
    # carry their candidate's stamp. Nothing is pending, so nothing is suppressed.
    cache("0.4.1rc1", channel="stable", latest_version="0.4.1rc1", channel_move_pending=False)
    assert up._display_local_version() == "0.4.1"
    assert up.status_update_fields()["update_channel_move_pending"] is False


def test_stable_build_running_behind_folds_and_is_not_a_move(cache):
    # One release behind is an ORDINARY available update, not a channel move --
    # the distinction the "is the local version newer" direction draws.
    cache("0.4.0rc14", channel="stable", latest_version="0.4.1rc1", channel_move_pending=False)
    assert up._display_local_version() == "0.4.0"
    assert up.status_update_fields()["update_channel_move_pending"] is False


@pytest.mark.parametrize("channel", ["insider", "nightly"])
def test_non_stable_channels_never_fold(cache, channel):
    cache("0.5.0rc3", channel=channel, latest_version="0.5.0rc3")
    assert up._display_local_version() == "0.5.0rc3"


def test_unchecked_install_keeps_the_historical_fold(cache):
    # No feed answer means UNKNOWN, and unknown must not un-fold the promoted
    # stable population on every boot before the first check lands.
    cache("0.4.1rc1", channel="stable")
    assert up._update_info["channel_move_pending"] is False
    assert up._display_local_version() == "0.4.1"


def test_feed_floor_uses_the_same_folded_local_version(cache):
    # The forced-update floor compares against the SAME folded value, so a
    # promoted stable build is never told to update to the release it already is.
    cache("0.4.1rc1", channel="stable", latest_version="0.4.1rc1", feed_min_version="0.4.1")
    assert up._feed_requires_update() is False
    cache("0.4.0rc14", channel="stable", latest_version="0.4.1rc1", feed_min_version="0.4.1")
    assert up._feed_requires_update() is True


def test_a_failed_or_feedless_check_reports_no_move(cache):
    # A git checkout, a desktop bundle and a container all reach here with no
    # channel and no feed version; a check that FAILED lands in the same shape.
    # None of them may be told they are mid-switch on a comparison never made.
    cache("0.5.0rc3", check_status=up.CHECK_FAILED, error_code=up.ERR_FEED_UNREACHABLE)
    assert up.status_update_fields()["update_channel_move_pending"] is False
    cache("0.6.0.dev20260829060906")
    assert up.status_update_fields()["update_channel_move_pending"] is False
