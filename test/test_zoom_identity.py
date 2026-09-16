"""Tests for the Zoom identity contract (W11-A).

Covers the numeric-id vs UUID distinction, the double-encoding rule and its
false-absence consequence, the instances-list series-id keying, the
occurrence-vs-parent-series distinction, and the no-local-time-inference rule.
"""

from urllib.parse import quote

import pytest

from kiro_crew.connections.control_plane import CREDENTIAL_MODES
from kiro_crew.connections.vendors.zoom.identity import (
    ZOOM_CREDENTIAL_MODES,
    ZOOM_SERVER_TO_SERVER,
    ZOOM_USER_OAUTH,
    OccurrenceTarget,
    encode_uuid_path_segment,
    needs_double_encoding,
    occurrence_target,
    resolve_occurrence_time,
    series_id_for_instances,
)


class TestDoubleEncoding:
    def test_slash_prefixed_uuid_needs_double_encoding(self):
        assert needs_double_encoding("/abc123==") is True

    def test_double_slash_uuid_needs_double_encoding(self):
        assert needs_double_encoding("aa//bb==") is True

    def test_ordinary_uuid_does_not_need_double_encoding(self):
        assert needs_double_encoding("aDYlohsHRtCd4ii1uC2+hA==") is False

    def test_encode_double_encodes_when_required(self):
        raw = "/abc=="
        out = encode_uuid_path_segment(raw)
        # single pass encodes '/' to %2F; the second pass encodes '%' to %25.
        assert "%252F" in out
        # exactly reproducible as quote(quote(raw, safe=''), safe='')
        assert out == quote(quote(raw, safe=""), safe="")

    def test_encode_single_encodes_ordinary_uuid(self):
        raw = "aDYlohsHRtCd4ii1uC2+hA=="
        out = encode_uuid_path_segment(raw)
        assert out == quote(raw, safe="")
        # a plus/equals get encoded once, not twice
        assert "%25" not in out


class TestSeriesIdForInstances:
    def test_accepts_numeric_meeting_id(self):
        assert series_id_for_instances("97763643886") == "97763643886"

    def test_accepts_display_formatted_numeric_id(self):
        assert series_id_for_instances("982 610 0285") == "982 610 0285"

    def test_refuses_occurrence_uuid_needing_double_encoding(self):
        with pytest.raises(ValueError):
            series_id_for_instances("/Vg8IdgluR5WDeWIkpJlElQ==")

    def test_refuses_base64_occurrence_uuid(self):
        with pytest.raises(ValueError):
            series_id_for_instances("Vg8IdgluR5WDeWIkpJlElQ==")


class TestOccurrenceTarget:
    def test_present_occurrence_id_targets_single_occurrence(self):
        assert occurrence_target("1648194360000") is OccurrenceTarget.SINGLE_OCCURRENCE

    def test_missing_occurrence_id_targets_parent_series(self):
        assert occurrence_target(None) is OccurrenceTarget.PARENT_SERIES

    def test_empty_occurrence_id_targets_parent_series(self):
        # negative fault: an update meant for one occurrence but arriving with an
        # empty occurrence_id hits the parent series, not the intended occurrence.
        assert occurrence_target("") is OccurrenceTarget.PARENT_SERIES


class TestOccurrenceTime:
    def test_binds_start_time_to_series_timezone(self):
        t = resolve_occurrence_time("2022-03-25T07:46:00Z", "America/Los_Angeles")
        assert t.start_time == "2022-03-25T07:46:00Z"
        assert t.timezone == "America/Los_Angeles"
        assert t.timezone_known is True

    def test_missing_timezone_is_carried_not_inferred(self):
        t = resolve_occurrence_time("2022-03-25T07:46:00Z", None)
        assert t.timezone is None
        assert t.timezone_known is False

    def test_empty_timezone_is_not_known(self):
        t = resolve_occurrence_time("2022-03-25T07:46:00Z", "")
        assert t.timezone_known is False


class TestCredentialMode:
    def test_zoom_modes_are_shared_control_plane_values(self):
        # Zoom reuses the shared W01 CredentialMode axis, not a parallel enum.
        assert ZOOM_USER_OAUTH == "oauth_user"
        assert ZOOM_SERVER_TO_SERVER == "service_to_service"
        assert ZOOM_USER_OAUTH in CREDENTIAL_MODES
        assert ZOOM_SERVER_TO_SERVER in CREDENTIAL_MODES

    def test_zoom_supports_two_of_three_modes_no_pat(self):
        assert ZOOM_CREDENTIAL_MODES == (ZOOM_USER_OAUTH, ZOOM_SERVER_TO_SERVER)
        assert "fine_grained_pat" not in ZOOM_CREDENTIAL_MODES
