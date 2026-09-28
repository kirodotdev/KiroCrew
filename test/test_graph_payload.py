"""Tests for Microsoft Graph request/response payload shaping (pure logic)."""

import pytest

from kiro_crew.connections.vendors.microsoft.graph.payload import (
    CalendarViewWindow,
    GraphPayloadError,
    QuerySpec,
    parse_collection,
    shape_request,
)


class TestQuerySpec:
    def test_empty_spec_is_empty_params(self):
        assert QuerySpec().to_query_params() == {}

    def test_all_odata_options(self):
        spec = QuerySpec(
            select=["id", "name"],
            filter_="startsWith(name,'a')",
            top=50,
            expand=["fields"],
            orderby=["createdDateTime desc"],
            search='"budget"',
            count=True,
        )
        params = spec.to_query_params()
        assert params["$select"] == "id,name"
        assert params["$filter"] == "startsWith(name,'a')"
        assert params["$top"] == "50"
        assert params["$expand"] == "fields"
        assert params["$orderby"] == "createdDateTime desc"
        assert params["$search"] == '"budget"'
        assert params["$count"] == "true"

    def test_top_must_be_positive(self):
        with pytest.raises(GraphPayloadError, match=r"\$top"):
            QuerySpec(top=0).to_query_params()
        with pytest.raises(GraphPayloadError, match=r"\$top"):
            QuerySpec(top=-1).to_query_params()

    def test_params_are_deterministically_ordered(self):
        a = QuerySpec(select=["id"], filter_="x eq 1", top=10).to_query_params()
        assert list(a.keys()) == sorted(a.keys())


class TestCalendarViewMandatoryWindow:
    def test_window_serializes(self):
        w = CalendarViewWindow(start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z")
        assert w.to_query_params() == {
            "startDateTime": "2026-01-01T00:00:00Z",
            "endDateTime": "2026-02-01T00:00:00Z",
        }

    def test_empty_start_is_rejected(self):
        with pytest.raises(GraphPayloadError, match="startDateTime"):
            CalendarViewWindow(start="  ", end="2026-02-01T00:00:00Z")

    def test_empty_end_is_rejected(self):
        with pytest.raises(GraphPayloadError, match="endDateTime"):
            CalendarViewWindow(start="2026-01-01T00:00:00Z", end="")

    def test_shape_request_refuses_calendar_view_path_without_window(self):
        with pytest.raises(GraphPayloadError, match="calendarView"):
            shape_request(method="get", path="/me/calendarView")

    def test_shape_request_merges_the_window(self):
        req = shape_request(
            method="get",
            path="/me/calendarView",
            calendar_view=CalendarViewWindow(
                start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z"
            ),
        )
        assert req.params["startDateTime"] == "2026-01-01T00:00:00Z"
        assert req.params["endDateTime"] == "2026-02-01T00:00:00Z"


class TestShapeRequest:
    def test_method_is_uppercased(self):
        assert shape_request(method="get", path="/me").method == "GET"

    def test_non_calendar_view_path_does_not_require_window(self):
        request = shape_request(method="get", path="/me/events")
        assert request.params == {}

    def test_params_are_read_only(self):
        req = shape_request(method="get", path="/me/events", query=QuerySpec(top=10))
        with pytest.raises(TypeError):
            req.params["$top"] = "20"

    def test_merges_query_and_calendar_view(self):
        req = shape_request(
            method="get",
            path="/me/calendarView",
            query=QuerySpec(select=["subject"], top=10),
            calendar_view=CalendarViewWindow(
                start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z"
            ),
        )
        assert req.params["$select"] == "subject"
        assert req.params["$top"] == "10"
        assert "startDateTime" in req.params


class TestParseCollection:
    def test_plain_next_link(self):
        page = parse_collection({"value": [{"id": "1"}], "@odata.nextLink": "https://graph/next"})
        assert page.value == [{"id": "1"}]
        assert page.next_link == "https://graph/next"
        assert page.delta_link is None

    def test_delta_link_terminator(self):
        page = parse_collection({"value": [], "@odata.deltaLink": "https://graph/delta"})
        assert page.delta_link == "https://graph/delta"
        assert page.next_link is None

    def test_last_page_has_neither_link(self):
        page = parse_collection({"value": [{"id": "1"}]})
        assert page.next_link is None
        assert page.delta_link is None

    def test_count_is_read(self):
        page = parse_collection({"value": [], "@odata.count": 42})
        assert page.count == 42

    def test_missing_value_is_rejected(self):
        with pytest.raises(GraphPayloadError, match="missing 'value'"):
            parse_collection({"@odata.nextLink": "x"})

    def test_non_array_value_is_rejected(self):
        with pytest.raises(GraphPayloadError, match="must be an array"):
            parse_collection({"value": {"id": "1"}})

    @pytest.mark.parametrize("key", ["@odata.nextLink", "@odata.deltaLink"])
    @pytest.mark.parametrize("value", [None, "", "   ", 42])
    def test_present_link_must_be_a_non_empty_string(self, key, value):
        with pytest.raises(GraphPayloadError, match="non-empty string"):
            parse_collection({"value": [], key: value})

    def test_both_links_present_is_rejected(self):
        # Contradictory paging state: Graph never emits both at once.
        with pytest.raises(GraphPayloadError, match="both @odata.nextLink"):
            parse_collection(
                {
                    "value": [],
                    "@odata.nextLink": "https://graph/next",
                    "@odata.deltaLink": "https://graph/delta",
                }
            )
