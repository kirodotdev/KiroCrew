"""A cron run's result row carries ``meta.turn_stats``, so the chat footer shows its usage.

Chat turns get the footer from ``chat_runner._attach_turn_stats``. A cron run's
result reaches the slot through ``inject_cron_result_to_dashboard`` instead, and
it stamps the same shape on the cron result row.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from chat_test_helpers import _make_state
from test_cron_context_meter_seed import _inject
from test_cron_context_meter_seed import _make_job as _make_mock_job
from test_cron_thread_routing import _make_gateway, _make_job, _run_callback

from kiro_crew.acp.types import TurnUsage
from kiro_crew.dashboard.chat_runner import turn_stats_meta
from kiro_crew.dashboard.state import _ChatSlot


def test_cron_run_hands_its_usage_to_the_result_row() -> None:
    gateway = _make_gateway()
    gateway.slack.post_blocks = AsyncMock(return_value="1711957800.001234")
    job = _make_job(persistent_session=True)
    with (
        patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard") as mock_inject,
        patch(
            "kiro_crew.slack.gateway.provider_last_turn_usage",
            return_value=TurnUsage(credits=1.23456, duration_ms=4200),
        ),
        patch("kiro_crew.slack.gateway.read_reported_turn_model", return_value="claude-sonnet-4.5"),
        patch("kiro_crew.slack.gateway.persist_token_record_async", new_callable=AsyncMock),
    ):
        _run_callback(gateway, job, stream_result="cron output")
    mock_inject.assert_called_once()
    assert mock_inject.call_args.kwargs["turn_stats"] == {
        "elapsed_ms": 4200,
        "credits": 1.2346,
        "model": "claude-sonnet-4.5",
    }


def test_silent_cron_hands_the_real_turn_stats_to_the_result_row() -> None:
    # The silent and duplicate-suppressed inject sites are only asserted with
    # turn_stats=ANY in test_cron_thread_routing, so swapping them to None would
    # stay green. Pin the real value on the silent path to close that gap.
    gateway = _make_gateway()
    gateway.dashboard_state.has_slot = MagicMock(return_value=True)
    job = _make_job(persistent_session=True, silent=True)
    with (
        patch("kiro_crew.slack.gateway.inject_cron_result_to_dashboard") as mock_inject,
        patch(
            "kiro_crew.slack.gateway.provider_last_turn_usage",
            return_value=TurnUsage(credits=1.23456, duration_ms=4200),
        ),
        patch("kiro_crew.slack.gateway.read_reported_turn_model", return_value="claude-sonnet-4.5"),
        patch("kiro_crew.slack.gateway.persist_token_record_async", new_callable=AsyncMock),
    ):
        _run_callback(gateway, job, stream_result="silent output")
    mock_inject.assert_called_once()
    assert mock_inject.call_args.kwargs["turn_stats"] == {
        "elapsed_ms": 4200,
        "credits": 1.2346,
        "model": "claude-sonnet-4.5",
    }


def test_injected_result_row_carries_turn_stats(tmp_path) -> None:
    state = _make_state(tmp_path)
    stats = turn_stats_meta(4200, 1.5, 0.0, "auto")
    real_append = _ChatSlot.append
    with patch.object(_ChatSlot, "append", autospec=True, side_effect=real_append) as spy:
        _inject(state, _make_mock_job(), "cron result", turn_stats=stats)
    # The stats ride the append itself, so the live broadcast an open tab
    # receives carries the footer, not only the stored row.
    appended_metas = [c.kwargs.get("meta") for c in spy.call_args_list if c.args[1] == "assistant"]
    assert appended_metas == [{"turn_stats": stats}]
    slot = state.get_slot("cron-abc123")
    result_rows = [m for m in slot.messages if m.get("role") == "assistant"]
    assert len(result_rows) == 1
    assert result_rows[0]["meta"]["turn_stats"] == {
        "elapsed_ms": 4200,
        "credits": 1.5,
        "model": "auto",
    }
    # The prompt row is not the reply: the footer belongs to the result only.
    assert not any(
        "turn_stats" in (m.get("meta") or {}) for m in slot.messages if m.get("role") == "user"
    )


def test_durable_cron_row_carries_turn_stats(tmp_path) -> None:
    # The durable cron:{id} copy is written straight away; the slot save that
    # also carries meta runs later. A restart in between must keep the footer.
    state = _make_state(tmp_path)
    stats = turn_stats_meta(4200, 1.5, 0.0, "auto")
    _inject(state, _make_mock_job(), "cron result", turn_stats=stats)
    on_disk = state.conversation_log.read_messages("cron:abc123")
    by_role = {m["role"]: m for m in on_disk}
    assert by_role["assistant"]["meta"]["turn_stats"] == stats
    assert by_role["assistant"]["meta"]["mid"]
    assert "turn_stats" not in (by_role["user"].get("meta") or {})


def test_stats_drop_non_finite_numbers_and_unbounded_models() -> None:
    stats = turn_stats_meta(10, float("inf"), float("nan"), "m" * 257)
    assert stats == {"elapsed_ms": 10}
    assert turn_stats_meta(10, 1.0, 0.0, "m" * 256) == {
        "elapsed_ms": 10,
        "credits": 1.0,
        "model": "m" * 256,
    }


def test_stats_redact_a_credential_shaped_model() -> None:
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    stats = turn_stats_meta(10, 0.0, 0.0, key)
    assert stats is not None
    assert key not in stats.get("model", "")


def test_stats_redact_a_url_shaped_model() -> None:
    url = "https://evil.example.com/c?d=" + "A" * 120
    stats = turn_stats_meta(10, 0.0, 0.0, url)
    assert stats is not None
    assert "evil.example.com/c?d=" not in stats.get("model", "")


def test_replay_without_stats_stamps_nothing(tmp_path) -> None:
    state = _make_state(tmp_path)
    _inject(state, _make_mock_job(), "cron result")
    slot = state.get_slot("cron-abc123")
    assert not any("turn_stats" in (m.get("meta") or {}) for m in slot.messages)
