"""The advance notice of the next update (``announce.json``) and its place in the check.

The notice is display metadata for the About page's "Upcoming update" block. A
missing, malformed or oversized file must leave the check exactly as it was, and
nothing here may decide whether or when anything installs.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from update_check_install_aware_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _wheel_install,
)

from kiro_crew.dashboard.handlers import _update_announce as announce
from kiro_crew.dashboard.handlers import updates

_FEED = {
    "channel": "insider",
    "pub_date": "2026-08-05T07:49:33Z",
    "schema": "kirocrew-cli-artifact-manifest-v1",
    "version": "0.1.3rc2",
}
_NOTICE = {
    "schema": announce.ANNOUNCE_SCHEMA,
    "channel": "insider",
    "version": "0.1.4rc1",
    "date": "2026-08-12",
    "notes": "## Added\n- **Faster** startup\n- `kirocrew doctor` is clearer\n- Third\n- Fourth\n",
    "notes_url": "https://github.com/kirodotdev/KiroCrew/releases/tag/v0.1.4",
}


def _bytes(doc: object) -> bytes:
    return json.dumps(doc).encode()


def _serve(monkeypatch, *, feed: object = _FEED, notice: bytes | int | Exception = 404):
    """Answer the manifest URL with ``feed`` and the announcement URL with ``notice``."""
    urls: list[str] = []

    async def _fake(url: str) -> tuple[int, bytes]:
        urls.append(url)
        if url.endswith("/announce.json"):
            if isinstance(notice, Exception):
                raise notice
            if isinstance(notice, int):
                return notice, b""
            return 200, notice
        return 200, _bytes(feed)

    monkeypatch.setattr(updates, "_fetch_feed_bytes", _fake)
    return urls


def _check(monkeypatch, local: str) -> dict:
    monkeypatch.setattr(updates, "_local_version", local)
    asyncio.run(updates._do_update_check())
    return updates.get_update_info()


class TestHighlights:
    def test_list_items_become_highlights_capped_at_three(self):
        got = announce.highlights_from_notes(_NOTICE["notes"])
        assert got == ["Faster startup", "kirocrew doctor is clearer", "Third"]

    def test_plain_lines_are_the_fallback_and_headings_are_skipped(self):
        assert announce.highlights_from_notes("# Title\n\nOne\nTwo\n") == ["One", "Two"]

    def test_a_long_item_is_clipped(self):
        got = announce.highlights_from_notes("- " + "word " * 80)
        assert len(got[0]) == announce.MAX_HIGHLIGHT_CHARS and got[0].endswith("…")

    def test_empty_notes_have_no_highlights(self):
        assert announce.highlights_from_notes("") == []


class TestParse:
    def test_a_valid_notice(self):
        got = announce.parse_announcement(_bytes(_NOTICE), channel="insider")
        assert got is not None
        assert got["version"] == "0.1.4rc1"
        assert got["date"] == "2026-08-12"
        assert got["notes_url"].startswith("https://github.com/")
        assert len(got["highlights"]) == 3

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b"not json",
            b"[1, 2]",
            b"null",
            _bytes({**_NOTICE, "schema": "other"}),
            _bytes({**_NOTICE, "channel": "stable"}),
            _bytes({**_NOTICE, "version": ""}),
            _bytes({**_NOTICE, "version": 7}),
            _bytes({**_NOTICE, "version": "x" * 200}),
            b"[" * 100000,
        ],
        ids=[
            "empty",
            "not-json",
            "array",
            "null",
            "other-schema",
            "other-channel",
            "empty-version",
            "numeric-version",
            "long-version",
            "deeply-nested",
        ],
    )
    def test_anything_else_is_no_notice(self, raw):
        assert announce.parse_announcement(raw, channel="insider") is None

    def test_bad_optional_fields_are_dropped_not_fatal(self):
        doc = {**_NOTICE, "date": "soon", "notes_url": "http://insecure.example/x", "notes": 5}
        got = announce.parse_announcement(_bytes(doc), channel="insider")
        assert got is not None
        assert (got["date"], got["notes_url"], got["highlights"]) == ("", "", [])

    @pytest.mark.parametrize("url", ["javascript:alert(1)", "https://", "https://a b", ""])
    def test_only_an_https_url_is_kept(self, url):
        assert announce.https_url(url) == ""


class TestCheckCarriesTheNotice:
    def test_no_file_and_nothing_newer_shows_nothing(self, monkeypatch):
        _serve(monkeypatch, notice=404)
        info = _check(monkeypatch, "0.1.3rc2")
        assert info["upcoming"] is None
        assert updates.status_update_fields()["update_upcoming"] is None

    @pytest.mark.parametrize(
        "notice", [b"{oops", b"[]", b"x" * 70000], ids=["broken-json", "array", "oversized"]
    )
    def test_a_malformed_or_oversized_file_changes_nothing(self, monkeypatch, notice):
        _serve(monkeypatch, notice=notice)
        info = _check(monkeypatch, "0.1.3rc2")
        assert info["check_status"] == "succeeded"
        assert info["upcoming"] is None

    def test_a_failing_fetch_never_raises_and_never_fails_the_check(self, monkeypatch):
        _serve(monkeypatch, notice=RuntimeError("boom"))
        info = _check(monkeypatch, "0.1.3rc2")
        assert info["check_status"] == "succeeded"
        assert info["upcoming"] is None

    def test_a_notice_for_a_newer_version_is_announced(self, monkeypatch):
        urls = _serve(monkeypatch, notice=_bytes(_NOTICE))
        info = _check(monkeypatch, "0.1.3rc2")
        assert urls[-1] == "https://updates.crew.kiro.dev/feed/insider/announce.json"
        assert info["update_available"] is False
        assert info["upcoming"] == {
            "state": "announced",
            "version": "0.1.4rc1",
            "date": "2026-08-12",
            "highlights": ["Faster startup", "kirocrew doctor is clearer", "Third"],
            "notes_url": _NOTICE["notes_url"],
        }
        assert updates.status_update_fields()["update_upcoming"] == info["upcoming"]

    def test_a_notice_for_the_installed_version_or_older_shows_nothing(self, monkeypatch):
        _serve(monkeypatch, notice=_bytes(_NOTICE))
        assert _check(monkeypatch, "0.1.4rc1")["upcoming"] is None

    def test_an_offered_build_adds_no_record_and_reads_no_notice(self, monkeypatch):
        feed = {**_FEED, "version": "0.1.4rc1"}
        urls = _serve(monkeypatch, feed=feed, notice=_bytes(_NOTICE))
        info = _check(monkeypatch, "0.1.3rc2")
        assert info["update_available"] is True
        assert info["upcoming"] is None
        assert not any(u.endswith("announce.json") for u in urls)
