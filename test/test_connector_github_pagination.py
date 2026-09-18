"""GitHub pagination wire-parsing tests, including malformed-header and
over-limit negative shapes. Pure functions -- no I/O, no side effects.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.github.pagination import (
    CURSOR_PARAM,
    MAX_PER_PAGE,
    REST_PAGE_PARAM,
    REST_PER_PAGE_PARAM,
    CursorPageRequest,
    RestPageRequest,
    clamp_per_page,
    next_page_url,
    parse_link_header,
)

# --- Link header parsing ---------------------------------------------------


def test_parse_link_header_next_and_last() -> None:
    header = (
        '<https://api.github.com/repositories/1/issues?page=2>; rel="next", '
        '<https://api.github.com/repositories/1/issues?page=9>; rel="last"'
    )
    links = parse_link_header(header)
    assert links["next"] == "https://api.github.com/repositories/1/issues?page=2"
    assert links["last"] == "https://api.github.com/repositories/1/issues?page=9"


def test_next_page_url_absent_on_last_page() -> None:
    # The last page has no Link header at all -- not an error, just the end.
    assert next_page_url(None) is None
    assert next_page_url("") is None
    assert next_page_url("   ") is None


def test_next_page_url_absent_when_only_prev_present() -> None:
    header = '<https://api.github.com/x?page=1>; rel="prev"'
    assert next_page_url(header) is None


def test_malformed_link_entry_missing_brackets_is_skipped_not_raised() -> None:
    # A single malformed entry must not lose the well-formed entries beside it.
    header = 'garbage-without-brackets; rel="next", <https://api.github.com/x?page=3>; rel="last"'
    links = parse_link_header(header)
    assert "next" not in links
    assert links["last"] == "https://api.github.com/x?page=3"


def test_malformed_link_entry_missing_rel_is_skipped() -> None:
    header = '<https://api.github.com/x?page=2>, <https://api.github.com/x?page=9>; rel="last"'
    links = parse_link_header(header)
    assert links == {"last": "https://api.github.com/x?page=9"}


def test_completely_malformed_link_header_yields_empty_mapping() -> None:
    assert parse_link_header("total nonsense, no brackets, no rel") == {}


def test_link_header_tolerates_extra_whitespace() -> None:
    header = '  <https://api.github.com/x?page=2>  ;  rel="next"  '
    assert next_page_url(header) == "https://api.github.com/x?page=2"


# --- per_page clamping (per_page > 100) ------------------------------------


@pytest.mark.parametrize(
    "requested,expected",
    [(1, 1), (30, 30), (100, 100), (101, 100), (500, 100), (10_000, 100), (0, 1), (-5, 1)],
)
def test_clamp_per_page(requested: int, expected: int) -> None:
    assert clamp_per_page(requested) == expected


def test_clamp_ceiling_is_100() -> None:
    assert MAX_PER_PAGE == 100
    assert clamp_per_page(MAX_PER_PAGE + 1) == MAX_PER_PAGE


# --- REST page request -----------------------------------------------------


def test_rest_page_request_clamps_per_page_on_construction() -> None:
    req = RestPageRequest(page=3, per_page=999)
    assert req.per_page == 100
    assert req.page == 3


def test_rest_page_request_raises_page_floor_to_one() -> None:
    assert RestPageRequest(page=0, per_page=50).page == 1


def test_rest_page_request_query_params_use_rest_names() -> None:
    params = RestPageRequest(page=2, per_page=50).as_query_params()
    assert params == {REST_PAGE_PARAM: 2, REST_PER_PAGE_PARAM: 50}
    assert CURSOR_PARAM not in params


# --- cursor request kept DISTINCT from REST params -------------------------


def test_cursor_request_first_page_omits_cursor_and_page() -> None:
    params = CursorPageRequest(after=None, per_page=50).as_query_params()
    assert params == {REST_PER_PAGE_PARAM: 50}
    # The whole point of the recorded contradiction: a cursor request never
    # emits the native REST page param.
    assert REST_PAGE_PARAM not in params
    assert CURSOR_PARAM not in params


def test_cursor_request_subsequent_page_uses_after_not_page() -> None:
    params = CursorPageRequest(after="Y3Vyc29yOnYyOpK5", per_page=50).as_query_params()
    assert params[CURSOR_PARAM] == "Y3Vyc29yOnYyOpK5"
    assert REST_PAGE_PARAM not in params


def test_cursor_and_rest_param_names_are_disjoint() -> None:
    # A generic connector must not collapse the two contracts into one "page
    # token" field; the param name spaces stay separate.
    assert CURSOR_PARAM != REST_PAGE_PARAM
    assert CURSOR_PARAM != REST_PER_PAGE_PARAM


def test_cursor_request_clamps_per_page() -> None:
    assert CursorPageRequest(after="c", per_page=250).per_page == 100
