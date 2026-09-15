"""Tests for the Microsoft Graph pagination / cursor protocol (pure logic).

These are the substantive negative tests for the slice: the engine must refuse
``n/a`` (NONE) and ``UNKNOWN`` modes, refuse a calendarView/filter-required first
page missing its precondition, and turn a 410 resync into a full re-enumeration
cursor rather than a terminal error.
"""

import pytest

from kiro_crew.connections.vendors.microsoft.graph.paging import (
    DELTA_TERMINATED_MODES,
    RESYNC_APPLY,
    RESYNC_UPLOAD,
    PagingError,
    PagingMode,
    assert_pageable,
    check_first_page_preconditions,
    next_step,
    parse_resync,
)
from kiro_crew.connections.vendors.microsoft.graph.payload import GraphPage


class TestPageabilityGate:
    @pytest.mark.parametrize(
        "mode",
        [
            PagingMode.NEXT_LINK,
            PagingMode.TOP_SERVER_DRIVEN,
            PagingMode.NEXT_LINK_THEN_DELTA,
            PagingMode.CALENDAR_VIEW,
            PagingMode.FILTER_REQUIRED,
            PagingMode.DELTA_RESYNC,
        ],
    )
    def test_pageable_modes(self, mode):
        assert_pageable(mode)  # does not raise

    def test_none_mode_is_not_pageable(self):
        # 38 of 73 catalog ops are pagination n/a: the engine must NOT be
        # applied to them by default.
        with pytest.raises(PagingError, match="n/a"):
            assert_pageable(PagingMode.NONE)

    def test_unknown_mode_is_not_pageable_and_distinct_from_none(self):
        # The one preserved unknown: do not assume it paginates or fabricate a
        # nextLink. Its refusal reason differs from NONE's on purpose.
        with pytest.raises(PagingError, match="UNKNOWN|do not assume"):
            assert_pageable(PagingMode.UNKNOWN)


class TestFirstPagePreconditions:
    def test_calendar_view_requires_both_window_params(self):
        with pytest.raises(PagingError, match="calendarView"):
            check_first_page_preconditions(PagingMode.CALENDAR_VIEW, {})
        with pytest.raises(PagingError, match="endDateTime"):
            check_first_page_preconditions(
                PagingMode.CALENDAR_VIEW, {"startDateTime": "2026-01-01T00:00:00Z"}
            )

    def test_calendar_view_passes_with_full_window(self):
        check_first_page_preconditions(
            PagingMode.CALENDAR_VIEW,
            {"startDateTime": "2026-01-01T00:00:00Z", "endDateTime": "2026-02-01T00:00:00Z"},
        )

    def test_filter_required_refuses_missing_filter(self):
        with pytest.raises(PagingError, match=r"\$filter"):
            check_first_page_preconditions(PagingMode.FILTER_REQUIRED, {})

    def test_filter_required_passes_with_filter(self):
        check_first_page_preconditions(PagingMode.FILTER_REQUIRED, {"$filter": "x eq 1"})

    def test_plain_modes_have_no_precondition(self):
        check_first_page_preconditions(PagingMode.NEXT_LINK, {})
        check_first_page_preconditions(PagingMode.TOP_SERVER_DRIVEN, {})

    def test_precondition_check_also_refuses_none_and_unknown(self):
        with pytest.raises(PagingError):
            check_first_page_preconditions(PagingMode.NONE, {})
        with pytest.raises(PagingError):
            check_first_page_preconditions(PagingMode.UNKNOWN, {})


class TestNextStep:
    def test_plain_next_link_continues_verbatim(self):
        page = GraphPage(value=[{"id": "1"}], next_link="https://graph/skiptoken=abc")
        step = next_step(PagingMode.NEXT_LINK, page)
        assert step.done is False
        assert step.next_cursor == "https://graph/skiptoken=abc"

    def test_next_link_exhausted_completes(self):
        step = next_step(PagingMode.NEXT_LINK, GraphPage(value=[]))
        assert step.done is True
        assert step.next_cursor is None
        assert step.delta_cursor is None

    def test_top_server_driven_does_not_reappend_top(self):
        # The opaque nextLink already encodes the page size; the cursor is the
        # link verbatim, so $top is never re-appended.
        page = GraphPage(value=[{"id": "1"}], next_link="https://graph/next?$skiptoken=t")
        step = next_step(PagingMode.TOP_SERVER_DRIVEN, page)
        assert step.next_cursor == "https://graph/next?$skiptoken=t"
        assert "$top" not in step.next_cursor.split("?", 1)[1]

    @pytest.mark.parametrize("mode", DELTA_TERMINATED_MODES)
    def test_delta_terminated_mode_terminates_on_delta(self, mode):
        # nextLink absent, deltaLink present -> done, but resumable via the
        # delta cursor (not discarded).
        page = GraphPage(value=[{"id": "1"}], delta_link="https://graph/deltatoken=d")
        step = next_step(mode, page)
        assert step.done is True
        assert step.delta_cursor == "https://graph/deltatoken=d"

    @pytest.mark.parametrize("mode", DELTA_TERMINATED_MODES)
    def test_delta_terminated_mode_requires_terminal_delta_link(self, mode):
        with pytest.raises(PagingError, match="@odata.deltaLink"):
            next_step(mode, GraphPage(value=[]))

    def test_next_link_then_delta_continues_while_next_link_present(self):
        page = GraphPage(value=[], next_link="https://graph/next")
        step = next_step(PagingMode.NEXT_LINK_THEN_DELTA, page)
        assert step.done is False
        assert step.next_cursor == "https://graph/next"

    def test_next_step_refuses_none_mode(self):
        with pytest.raises(PagingError):
            next_step(PagingMode.NONE, GraphPage(value=[]))

    def test_next_step_refuses_unknown_mode(self):
        with pytest.raises(PagingError):
            next_step(PagingMode.UNKNOWN, GraphPage(value=[]))


class TestResync410:
    def test_apply_differences_from_error_object(self):
        signal = parse_resync(
            410,
            {"error": {"code": RESYNC_APPLY}},
            {"Location": "https://graph/fresh-delta"},
        )
        assert signal is not None
        assert signal.kind == RESYNC_APPLY
        # The fresh link IS the cursor to re-enumerate the whole collection from.
        assert signal.fresh_link == "https://graph/fresh-delta"

    def test_upload_differences_top_level_annotation(self):
        signal = parse_resync(
            410,
            {RESYNC_UPLOAD: True},
            {"location": "https://graph/fresh2"},  # header lookup is case-insensitive
        )
        assert signal is not None
        assert signal.kind == RESYNC_UPLOAD
        assert signal.fresh_link == "https://graph/fresh2"

    def test_non_410_is_not_this_functions_concern(self):
        # 401/403/404/429/5xx are W01's typed boundary, not a resync signal.
        assert parse_resync(404, {"error": {"code": "itemNotFound"}}, {}) is None
        assert parse_resync(429, {}, {"Retry-After": "5"}) is None

    def test_410_without_resync_annotation_is_not_a_resync(self):
        # A plain gone is W01's boundary, not a delta resync.
        assert parse_resync(410, {"error": {"code": "itemNotFound"}}, {}) is None

    def test_410_resync_missing_location_is_malformed(self):
        # A resync that claims to resync but names no fresh link must not be
        # silently treated as terminal.
        with pytest.raises(PagingError, match="Location"):
            parse_resync(410, {"error": {"code": RESYNC_APPLY}}, {})

    def test_410_resync_blank_location_is_malformed(self):
        with pytest.raises(PagingError, match="Location"):
            parse_resync(410, {RESYNC_APPLY: True}, {"Location": "   "})
