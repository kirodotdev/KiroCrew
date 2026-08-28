"""Plan rate-limit meta on a ``usage_update``: ``_meta["_claude/rateLimit"]``.

claude-agent-acp forwards the Claude Code SDK's rate-limit block verbatim, and
emits it only when the state CHANGES. Two consequences these tests pin, because
both fail silently: a partially-malformed block must keep the fields that did
parse (the frame is the only one that will carry them), and the reading must
survive a turn boundary rather than blanking until the next change event.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.acp import _dispatch
from kiro_crew.acp._dispatch import parse_rate_limit
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    META_CLAUDE_RATE_LIMIT,
    RATE_LIMIT_STATES,
    AcpPromptStats,
    AcpRateLimit,
    JsonRpcMessage,
)


def _frame(block, *, used=50_000, size=200_000):
    """A ``usage_update`` payload carrying ``block`` as its rate-limit meta."""
    return {
        "sessionUpdate": "usage_update",
        "used": used,
        "size": size,
        "_meta": {META_CLAUDE_RATE_LIMIT: block},
    }


class TestParseRateLimitHappyPath:
    def test_reads_every_field(self):
        parsed = parse_rate_limit(
            _frame(
                {
                    "status": "allowed_warning",
                    "rateLimitType": "five_hour",
                    "utilization": 82.5,
                    "resetsAt": 1_770_000_000,
                }
            )
        )
        assert parsed == AcpRateLimit(
            status="allowed_warning",
            limit_type="five_hour",
            utilization=82.5,
            resets_at=1_770_000_000.0,
        )

    @pytest.mark.parametrize("status", RATE_LIMIT_STATES)
    def test_every_known_state_survives(self, status):
        # The set is what the dashboard maps to a severity; a state that parsed
        # to "" would render at the no-verdict fallback instead of its own.
        parsed = parse_rate_limit(_frame({"status": status}))
        assert parsed is not None and parsed.status == status

    def test_utilization_alone_is_a_reading(self):
        # No status, one number: the honest degrade is the figure with no verdict,
        # not a discarded frame.
        parsed = parse_rate_limit(_frame({"utilization": 12.0}))
        assert parsed is not None
        assert (parsed.status, parsed.utilization) == ("", 12.0)

    def test_zero_utilization_is_reported_not_absent(self):
        # 0.0 means "window untouched" and must not collide with the -1.0
        # not-reported sentinel.
        parsed = parse_rate_limit(_frame({"utilization": 0}))
        assert parsed is not None and parsed.utilization == 0.0
        assert parsed.to_payload()["utilization"] == 0.0


class TestParseRateLimitAbsence:
    @pytest.mark.parametrize(
        "update",
        [
            {"sessionUpdate": "usage_update", "used": 1, "size": 2},  # no _meta at all
            {"sessionUpdate": "usage_update", "_meta": "nope"},  # _meta not a dict
            {"sessionUpdate": "usage_update", "_meta": {}},  # meta without our key
            {"sessionUpdate": "usage_update", "_meta": {META_CLAUDE_RATE_LIMIT: "nope"}},
            {"sessionUpdate": "usage_update", "_meta": {META_CLAUDE_RATE_LIMIT: None}},
        ],
    )
    def test_no_usable_meta_returns_none(self, update):
        assert parse_rate_limit(update) is None

    def test_non_dict_update_returns_none(self):
        assert parse_rate_limit("usage_update") is None  # type: ignore[arg-type]

    def test_block_with_nothing_usable_returns_none(self):
        # None, not an all-defaults instance: a caller must be able to tell
        # "the frame said nothing" from "the quota is at 0%".
        assert parse_rate_limit(_frame({"utilization": "eighty", "status": 7})) is None

    def test_resets_at_alone_is_not_a_reading(self):
        # A reset time with no window name, verdict or figure names nothing the
        # popover can label, so it is not a reading (AcpRateLimit.is_reported).
        assert parse_rate_limit(_frame({"resetsAt": 1_770_000_000})) is None


class TestParseRateLimitValidatesFieldsIndependently:
    def test_unrecognised_status_is_dropped_and_logged(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=_dispatch.logger.name):
            parsed = parse_rate_limit(_frame({"status": "throttled_soon", "utilization": 91.0}))
        # The good field survives; the unknown verdict does not become one.
        assert parsed is not None
        assert (parsed.status, parsed.utilization) == ("", 91.0)
        assert any("unrecognised status" in r.getMessage() for r in caplog.records)

    def test_malformed_utilization_keeps_the_other_fields(self):
        # The regression shape: one bad field must not discard a frame that also
        # carried the reset time, because no later frame repeats it.
        parsed = parse_rate_limit(
            _frame({"status": "rejected", "utilization": float("nan"), "resetsAt": 1_770_000_000})
        )
        assert parsed is not None
        assert parsed.status == "rejected"
        assert parsed.utilization == -1.0
        assert parsed.resets_at == 1_770_000_000.0

    @pytest.mark.parametrize("bad", [True, "82", None, [82], float("inf"), 10**400])
    def test_non_numeric_utilization_is_not_reported(self, bad):
        parsed = parse_rate_limit(_frame({"status": "allowed", "utilization": bad}))
        assert parsed is not None and parsed.utilization == -1.0
        assert "utilization" not in parsed.to_payload()

    @pytest.mark.parametrize("raw,expected", [(-5, 0.0), (140, 100.0)])
    def test_utilization_is_clamped_to_a_percentage(self, raw, expected):
        parsed = parse_rate_limit(_frame({"utilization": raw}))
        assert parsed is not None and parsed.utilization == expected

    def test_non_string_limit_type_is_dropped(self):
        parsed = parse_rate_limit(_frame({"status": "allowed", "rateLimitType": 5}))
        assert parsed is not None and parsed.limit_type == ""


class TestResetsAtUnitNormalization:
    """The SDK types ``resetsAt`` as a bare number and declares no unit, so the
    parser splits on magnitude. A ms value read as seconds renders a reset in the
    year 58,000; a seconds value read as ms renders one in 1970."""

    def test_milliseconds_are_normalized_to_seconds(self):
        parsed = parse_rate_limit(_frame({"status": "allowed", "resetsAt": 1_770_000_000_000}))
        assert parsed is not None and parsed.resets_at == 1_770_000_000.0

    def test_seconds_pass_through(self):
        parsed = parse_rate_limit(_frame({"status": "allowed", "resetsAt": 1_770_000_000}))
        assert parsed is not None and parsed.resets_at == 1_770_000_000.0

    @pytest.mark.parametrize("bad", [0, -1, "soon", None, True])
    def test_unusable_resets_at_is_not_reported(self, bad):
        parsed = parse_rate_limit(_frame({"status": "allowed", "resetsAt": bad}))
        assert parsed is not None and parsed.resets_at == 0.0
        assert "resets_at" not in parsed.to_payload()


class TestPayloadOmitsUnreportedFields:
    def test_sentinels_never_reach_the_wire(self):
        # -1.0 utilization would render as "-1%" and 0.0 resets_at as 1970.
        payload = AcpRateLimit(status="allowed").to_payload()
        assert payload == {"status": "allowed"}

    def test_payload_carries_what_was_reported(self):
        payload = AcpRateLimit(
            status="rejected", limit_type="seven_day", utilization=99.94, resets_at=1_770_000_000.0
        ).to_payload()
        assert payload == {
            "status": "rejected",
            "limit_type": "seven_day",
            # Rounded at the boundary: a quota reading is shown to one decimal,
            # and 99.94000000000001 is noise the frontend would have to strip.
            "utilization": 99.9,
            "resets_at": 1_770_000_000.0,
        }

    def test_empty_reading_serializes_empty(self):
        assert AcpRateLimit().to_payload() == {}
        assert AcpRateLimit().is_reported() is False


class TestClientConsumesTheMeta:
    def _msg(self, update):
        return JsonRpcMessage(method="session/update", params={"update": update})

    def test_usage_update_records_the_quota(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(
            self._msg(_frame({"status": "allowed_warning", "utilization": 76.0}))
        )
        stats = client.last_prompt_stats
        assert stats.rate_limit is not None
        assert (stats.rate_limit.status, stats.rate_limit.utilization) == ("allowed_warning", 76.0)
        # Read on an independent footing from the token pair: both land.
        assert stats.context_used_tokens == 50_000

    def test_quota_lands_even_when_the_token_counts_are_unusable(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._msg(_frame({"status": "rejected"}, used=None, size=None)))
        assert client.last_prompt_stats.rate_limit is not None
        assert client.last_prompt_stats.rate_limit.status == "rejected"

    def test_a_frame_without_the_meta_leaves_the_last_reading_alone(self, tmp_path):
        # The adapter emits the block only when the state CHANGES, so every
        # intervening usage frame omits it. Treating that as "quota unknown"
        # would blank the readout for the rest of the session.
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._msg(_frame({"status": "allowed_warning"})))
        client._track_usage_update(
            self._msg({"sessionUpdate": "usage_update", "used": 60_000, "size": 200_000})
        )
        assert client.last_prompt_stats.rate_limit is not None
        assert client.last_prompt_stats.rate_limit.status == "allowed_warning"


class TestSurvivesATurnBoundary:
    def test_carry_over_keeps_the_reading(self):
        stats = AcpPromptStats(
            context_pct=41.0,
            rate_limit=AcpRateLimit(status="allowed_warning", utilization=80.0),
        )
        carried = stats.carry_over()
        assert carried.rate_limit == stats.rate_limit

    def test_context_reset_does_not_clear_it(self):
        # Compaction changes the transcript, not the account: the quota is still
        # whatever the adapter last reported for it.
        stats = AcpPromptStats(
            context_used_tokens=180_000,
            context_window_tokens=200_000,
            rate_limit=AcpRateLimit(status="rejected"),
        )
        stats.reset_context_state()
        assert stats.rate_limit is not None and stats.rate_limit.status == "rejected"
        assert stats.context_used_tokens == 0
