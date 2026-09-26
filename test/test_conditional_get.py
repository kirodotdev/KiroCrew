"""``kiro_crew.dashboard.conditional_get`` -- the one ETag / 304 implementation.

Four media routes (appearance media, theme assets, app art, crew avatars) used
to carry their own copy of the compare and drifted: three (app art
``handle_app_art_file``, appearance ``_media_response``, the crew avatar GET)
compared the raw ``If-None-Match`` string with ``==``, which misses the weak
``W/"..."`` form and the list form. These tests pin the shared behaviour once
so a route only has to prove it calls the helper.
"""

from __future__ import annotations

from email.utils import formatdate

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.conditional_get import (
    bare_etag_value,
    conditional_response,
    is_not_modified,
    strong_content_etag,
    weak_content_etag,
)

BODY = b"<svg><style>.a{fill:red}</style></svg>"


def _req(**headers: str) -> web.Request:
    return make_mocked_request("GET", "/x", headers=headers)


class TestValidators:
    def test_strong_tag_is_quoted_sha256_prefix(self) -> None:
        tag = strong_content_etag(BODY)
        assert tag.startswith('"') and tag.endswith('"') and not tag.startswith("W/")
        assert len(bare_etag_value(tag)) == 32

    def test_weak_tag_is_w_prefixed_blake2b(self) -> None:
        tag = weak_content_etag(BODY)
        assert tag.startswith('W/"') and tag.endswith('"')
        assert len(bare_etag_value(tag)) == 16

    def test_a_byte_change_changes_both_validators(self) -> None:
        other = BODY + b" "
        assert strong_content_etag(BODY) != strong_content_etag(other)
        assert weak_content_etag(BODY) != weak_content_etag(other)

    @pytest.mark.parametrize(
        "header, value",
        [('"abc"', "abc"), ('W/"abc"', "abc"), ("abc", "abc"), (' W/"abc" ', "abc")],
    )
    def test_bare_value_strips_marker_and_quotes(self, header: str, value: str) -> None:
        assert bare_etag_value(header) == value


class TestIfNoneMatch:
    """RFC 9110 section 13.1.2: the WEAK comparison, ``*`` and lists."""

    @pytest.mark.parametrize("current", [strong_content_etag(BODY), weak_content_etag(BODY)])
    @pytest.mark.parametrize(
        "sent",
        [
            '"{v}"',  # strong form of the current value
            'W/"{v}"',  # weak form -- a proxy may add the marker
            '"zzz", "{v}"',  # a list, ours not first
            '"zzz", W/"{v}"',  # a list with the weak form
            "*",  # any current representation
        ],
    )
    def test_matching_forms_are_not_modified(self, current: str, sent: str) -> None:
        req = _req(**{"If-None-Match": sent.format(v=bare_etag_value(current))})
        assert is_not_modified(req, current) is True

    def test_a_different_tag_is_modified(self) -> None:
        req = _req(**{"If-None-Match": '"stale"'})
        assert is_not_modified(req, strong_content_etag(BODY)) is False

    def test_a_list_without_ours_is_modified(self) -> None:
        req = _req(**{"If-None-Match": '"a", W/"b"'})
        assert is_not_modified(req, weak_content_etag(BODY)) is False

    def test_a_malformed_header_is_modified(self) -> None:
        req = _req(**{"If-None-Match": "garbage"})
        assert is_not_modified(req, strong_content_etag(BODY)) is False

    def test_an_empty_header_is_modified(self) -> None:
        mtime = 1_700_000_000.75
        req = _req(
            **{
                "If-None-Match": "",
                "If-Modified-Since": formatdate(mtime, usegmt=True),
            }
        )
        assert is_not_modified(req, strong_content_etag(BODY), last_modified=mtime) is False

    def test_no_header_is_modified(self) -> None:
        assert is_not_modified(_req(), strong_content_etag(BODY)) is False


class TestIfModifiedSince:
    """Section 13.1.3: only consulted when no ``If-None-Match`` was sent, and
    only when the caller has a modification time to compare against."""

    MTIME = 1_700_000_000.75

    def test_unchanged_since_is_not_modified(self) -> None:
        req = _req(**{"If-Modified-Since": formatdate(self.MTIME, usegmt=True)})
        assert is_not_modified(req, '"x"', last_modified=self.MTIME) is True

    def test_sub_second_mtime_is_truncated_not_rounded(self) -> None:
        # HTTP dates have no sub-second part: the header says :00 and the file
        # was written at :00.75 -- the same second, so still unmodified.
        req = _req(**{"If-Modified-Since": formatdate(int(self.MTIME), usegmt=True)})
        assert is_not_modified(req, '"x"', last_modified=self.MTIME) is True

    def test_written_after_the_date_is_modified(self) -> None:
        req = _req(**{"If-Modified-Since": formatdate(self.MTIME - 60, usegmt=True)})
        assert is_not_modified(req, '"x"', last_modified=self.MTIME) is False

    def test_ignored_when_if_none_match_present(self) -> None:
        req = _req(
            **{
                "If-None-Match": '"stale"',
                "If-Modified-Since": formatdate(self.MTIME, usegmt=True),
            }
        )
        assert is_not_modified(req, '"x"', last_modified=self.MTIME) is False

    def test_ignored_when_malformed_if_none_match_present(self) -> None:
        req = _req(
            **{
                "If-None-Match": "garbage",
                "If-Modified-Since": formatdate(self.MTIME, usegmt=True),
            }
        )
        assert is_not_modified(req, '"x"', last_modified=self.MTIME) is False

    def test_ignored_when_empty_if_none_match_present(self) -> None:
        req = _req(
            **{
                "If-None-Match": "",
                "If-Modified-Since": formatdate(self.MTIME, usegmt=True),
            }
        )
        assert is_not_modified(req, '"x"', last_modified=self.MTIME) is False

    def test_ignored_when_caller_has_no_mtime(self) -> None:
        req = _req(**{"If-Modified-Since": formatdate(self.MTIME, usegmt=True)})
        assert is_not_modified(req, '"x"') is False


class TestConditionalResponse:
    CACHE = "private, max-age=0, must-revalidate"
    CSP = "default-src 'none'; sandbox"

    def _resp(self, **headers: str) -> web.Response:
        return conditional_response(
            _req(**headers),
            BODY,
            "image/svg+xml",
            etag=weak_content_etag(BODY),
            cache_control=self.CACHE,
            extra_headers={"Content-Security-Policy": self.CSP},
        )

    def test_a_200_carries_body_and_every_guard_header(self) -> None:
        resp = self._resp()
        assert resp.status == 200
        assert resp.body == BODY
        assert resp.content_type == "image/svg+xml"
        assert resp.headers["ETag"] == weak_content_etag(BODY)
        assert resp.headers["Cache-Control"] == self.CACHE
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Content-Security-Policy"] == self.CSP

    def test_a_304_repeats_the_same_headers_with_no_body(self) -> None:
        first = self._resp()
        again = self._resp(**{"If-None-Match": first.headers["ETag"]})
        assert again.status == 304
        assert again.body is None
        for name in ("ETag", "Cache-Control", "X-Content-Type-Options", "Content-Security-Policy"):
            assert again.headers[name] == first.headers[name], name

    def test_charset_rides_the_content_type(self) -> None:
        resp = conditional_response(
            _req(),
            b"<p>x</p>",
            "text/html",
            etag=strong_content_etag(b"<p>x</p>"),
            cache_control=self.CACHE,
            charset="utf-8",
        )
        assert resp.headers["Content-Type"] == "text/html; charset=utf-8"

    def test_no_extra_headers_is_fine(self) -> None:
        resp = conditional_response(
            _req(), BODY, "image/png", etag=strong_content_etag(BODY), cache_control="no-cache"
        )
        assert resp.status == 200
        assert "Content-Security-Policy" not in resp.headers


def test_every_media_route_uses_the_shared_helper() -> None:
    """The consolidation's fence: no route may grow its own compare again.

    A raw ``If-None-Match`` string compare is exactly the drift this module
    replaced, so any reappearance in the four route modules fails here.
    """
    import inspect

    from kiro_crew.apps import routes as apps_routes
    from kiro_crew.dashboard.handlers import agents, appearances, themes

    for mod in (apps_routes, agents, appearances, themes):
        src = inspect.getsource(mod)
        assert 'headers.get("If-None-Match")' not in src, mod.__name__
        assert "request.if_none_match" not in src, mod.__name__
