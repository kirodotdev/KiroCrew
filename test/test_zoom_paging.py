"""Tests for the Zoom pagination contract (W11-A).

Covers the four disciplines, the date-windowed cursor's fixed window, the
cursor-less refusal (negative fault test 3), and the month-iterated report.
"""

import pytest

from kiro_crew.connections.vendors.zoom.paging import (
    Pagination,
    classify_pagination,
    next_cursor_request,
    to_next_cursor,
)


class TestClassifyPagination:
    def test_list_endpoint_is_cursor(self):
        assert classify_pagination("zoom.meetings.list") is Pagination.CURSOR

    def test_recordings_list_is_date_windowed_cursor(self):
        assert classify_pagination("zoom.recordings.list") is Pagination.DATE_WINDOWED_CURSOR

    def test_instances_list_is_cursorless(self):
        assert classify_pagination("zoom.meetings.recurring_instances") is Pagination.CURSORLESS

    def test_single_resource_get_is_cursorless(self):
        assert classify_pagination("zoom.meetings.get") is Pagination.CURSORLESS

    def test_daily_report_is_month_iterated(self):
        assert classify_pagination("zoom.reports.daily_usage") is Pagination.MONTH_ITERATED

    def test_unknown_operation_refused_not_defaulted(self):
        # never silently assume "cursor" for an unclassified endpoint.
        with pytest.raises(KeyError):
            classify_pagination("zoom.something.unknown")

    def test_is_cursor_paged_property(self):
        assert Pagination.CURSOR.is_cursor_paged is True
        assert Pagination.DATE_WINDOWED_CURSOR.is_cursor_paged is True
        assert Pagination.CURSORLESS.is_cursor_paged is False
        assert Pagination.MONTH_ITERATED.is_cursor_paged is False


class TestNextCursorRequest:
    def test_plain_cursor_request(self):
        req = next_cursor_request("zoom.meetings.list", 30)
        assert req.page_size == 30
        assert req.next_page_token is None
        assert req.date_from is None and req.date_to is None

    def test_plain_cursor_carries_token(self):
        req = next_cursor_request("zoom.meetings.list", 30, next_page_token="abc")
        assert req.next_page_token == "abc"

    def test_date_windowed_requires_both_dates(self):
        with pytest.raises(ValueError):
            next_cursor_request("zoom.recordings.list", 30)

    def test_date_windowed_carries_fixed_window(self):
        req = next_cursor_request(
            "zoom.recordings.list",
            30,
            next_page_token="tok",
            date_from="2021-01-01",
            date_to="2021-02-01",
        )
        assert req.date_from == "2021-01-01"
        assert req.date_to == "2021-02-01"
        assert req.next_page_token == "tok"

    def test_plain_cursor_refuses_a_date_window(self):
        with pytest.raises(ValueError):
            next_cursor_request("zoom.meetings.list", 30, date_from="2021-01-01", date_to="x")

    def test_nonpositive_page_size_refused(self):
        with pytest.raises(ValueError):
            next_cursor_request("zoom.meetings.list", 0)


class TestCursorlessRefusal:
    """Negative fault test 3: a cursor is never followed on a cursor-less
    endpoint or a month-iterated report."""

    def test_refuses_cursor_on_instances_list(self):
        with pytest.raises(ValueError):
            next_cursor_request("zoom.meetings.recurring_instances", 30)

    def test_refuses_cursor_on_single_resource_get(self):
        with pytest.raises(ValueError):
            next_cursor_request("zoom.meetings.get", 30)

    def test_refuses_cursor_on_daily_report(self):
        with pytest.raises(ValueError):
            next_cursor_request("zoom.reports.daily_usage", 30)


class TestToNextCursor:
    """The fold into the control plane's opaque OperationResult.next_cursor."""

    def test_present_token_becomes_opaque_cursor(self):
        assert to_next_cursor("abc3445rg") == "abc3445rg"

    def test_absent_token_is_none_terminal_page(self):
        assert to_next_cursor(None) is None

    def test_empty_token_is_none_terminal_page(self):
        assert to_next_cursor("") is None
