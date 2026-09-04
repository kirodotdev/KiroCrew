"""Coverage tests for the telemetry panel handlers (``dashboard/handlers/telemetry.py``).

``test/metrics/test_telemetry_handler.py`` drives the aggregation math over synthetic
shards (``_pct_from_buckets`` / ``_Hist`` / ``_aggregate``). What had no tests is
everything AROUND that math — the parts that decide which files are read, what the
three HTTP routes report, and how a conversation title reaches the cost ranking:

  * ``_telemetry_cfg`` — the effective-vs-stored posture: the env pin overriding the
    config flag, the custom ``local_dir``, ``otlp_configured`` as presence-only, and
    the two failure paths that must report "off" rather than raise;
  * ``_shards_in_window`` — the missing directory, the sensitive-path refusal (a
    user-configurable ``local_dir`` must not read ``~/.ssh``), the per-shard symlink
    check, the window cutoff, and the two filename-shape rejections;
  * ``_parse_startup_metrics`` — the empty-window shape and the shard-fingerprint
    cache (a hit returns the same object; an appended line invalidates it);
  * ``_context_block`` / ``_cost_block`` — best-effort: an unreadable row store must
    return None, not take the OTEL sections down;
  * the three routes — ``/api/telemetry/startup``, ``/api/telemetry/beacon``
    (including its "never 500 on a broken config" fallback) and
    ``/api/telemetry/collection``, plus ``/api/telemetry/context-trace``'s 400;
  * ``_persisted_titles`` / ``_with_conversation_titles`` — the live-slot title, the
    persisted fallback, the placeholder filter, the redaction at this second
    serialization boundary, and the copy that keeps a title out of the memoised
    cost object;
  * ``_telemetry_overlay_pins`` — the ``config.local.json`` shadow report.

No network, no real config: shards are written under ``tmp_path``, the config loader
and beacon module are stubbed, and the module's shard cache is reset around every
test so ordering cannot matter.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.config import loader as config_loader
from kiro_crew.dashboard.handlers import telemetry as h
from kiro_crew.dashboard.state import NEW_SESSION_TITLE

_BOUNDS: list[float] = [10.0, 20.0, 30.0, 40.0, 50.0]


@pytest.fixture(autouse=True)
def _reset_shard_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The aggregation cache is module-global — never let it cross tests."""
    monkeypatch.setattr(h, "_CACHE", None)
    monkeypatch.setattr(h, "_CACHE_KEY", None)
    monkeypatch.setattr(h, "_CACHE_TS", 0.0)


def _cfg_stub(
    *,
    enabled: bool = True,
    local_dir: str | None = None,
    otlp_endpoint: str = "",
    beacon_enabled: bool = True,
    beacon_endpoint: str = "https://beacon.invalid/x",
    privacy_acked: bool = True,
) -> Any:
    telemetry = SimpleNamespace(
        enabled=enabled,
        local_dir=local_dir,
        otlp_endpoint=otlp_endpoint,
        beacon_enabled=beacon_enabled,
        beacon_endpoint=beacon_endpoint,
    )
    return SimpleNamespace(
        telemetry=telemetry, dashboard=SimpleNamespace(privacy_acked=privacy_acked)
    )


def _patch_config(monkeypatch: pytest.MonkeyPatch, cfg: Any) -> None:
    loader = MagicMock()
    loader.load = MagicMock(return_value=cfg)
    monkeypatch.setattr(h, "KiroCrewConfig", loader)


def _shard(directory: Path, day: str, metrics: list[dict], pid: int = 4242) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    line = {"resource_metrics": [{"scope_metrics": [{"metrics": metrics}]}]}
    path = directory / f"metrics-{day}-{pid}.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return path


def _startup_metric(bucket: int = 2, count: int = 2) -> list[dict]:
    counts = [0] * (len(_BOUNDS) + 1)
    counts[bucket] = count
    return [
        {
            "name": h._STARTUP_METRIC,
            "data": {
                "data_points": [
                    {
                        "attributes": {"outcome": "ready", "spawned": True},
                        "count": count,
                        "sum": 30.0,
                        "min": 15.0,
                        "max": 15.0,
                        "bucket_counts": counts,
                        "explicit_bounds": _BOUNDS,
                    }
                ]
            },
        }
    ]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _mk(path: str = "/api/telemetry/startup", *, state: Any = ..., query: str = "") -> web.Request:
    app = web.Application()
    if state is not ...:
        app["state"] = state
    return make_mocked_request("GET", f"{path}{query}", app=app)


def _body(response: web.StreamResponse) -> Any:
    assert isinstance(response, web.Response)
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


# --- _telemetry_cfg ----------------------------------------------------------


def test_telemetry_cfg_reports_stored_flag_and_custom_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_config(
        monkeypatch,
        _cfg_stub(enabled=True, local_dir=str(tmp_path / "m"), otlp_endpoint=" https://o "),
    )
    monkeypatch.setattr(h, "env_pin", lambda: None)
    state = h._telemetry_cfg()
    assert state.enabled is True
    assert state.env_pinned is False
    assert state.directory == tmp_path / "m"
    # Presence only — the endpoint itself may carry credentials and never leaves.
    assert state.otlp_configured is True


def test_telemetry_cfg_env_pin_overrides_the_stored_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, _cfg_stub(enabled=True))
    monkeypatch.setattr(h, "env_pin", lambda: False)
    state = h._telemetry_cfg()
    assert (state.enabled, state.env_pinned) == (False, True)
    assert state.env_var == h.TELEMETRY_ENV_VAR


def test_telemetry_cfg_reports_disabled_when_the_config_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail toward off: never claim collection is on when it cannot be proven."""
    loader = MagicMock()
    loader.load = MagicMock(side_effect=RuntimeError("unreadable"))
    monkeypatch.setattr(h, "KiroCrewConfig", loader)
    monkeypatch.setattr(h, "env_pin", lambda: None)
    state = h._telemetry_cfg()
    assert state.enabled is False
    assert state.otlp_configured is False


def test_telemetry_cfg_survives_a_failing_env_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, _cfg_stub(enabled=True))

    def _boom() -> bool:
        raise RuntimeError("pin resolution failed")

    monkeypatch.setattr(h, "env_pin", _boom)
    state = h._telemetry_cfg()
    assert (state.enabled, state.env_pinned) == (True, False)


# --- _shards_in_window -------------------------------------------------------


def test_shards_in_window_missing_directory_is_empty(tmp_path: Path) -> None:
    assert h._shards_in_window(tmp_path / "absent", 14) == []


def test_shards_in_window_refuses_a_sensitive_metrics_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``telemetry.local_dir`` is user-configurable, so a ~/.ssh alias must be refused."""
    _shard(tmp_path, _today(), _startup_metric())
    monkeypatch.setattr(h, "validate_file_path", lambda _p: None)
    assert h._shards_in_window(tmp_path, 14) == []


def test_shards_in_window_skips_a_shard_failing_the_path_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shard = _shard(tmp_path, _today(), _startup_metric())
    monkeypatch.setattr(
        h, "validate_file_path", lambda p: None if p == str(shard) else str(tmp_path)
    )
    assert h._shards_in_window(tmp_path, 14) == []


def test_shards_in_window_keeps_only_dates_inside_the_window(tmp_path: Path) -> None:
    fresh = _shard(tmp_path, _today(), _startup_metric(), pid=1)
    old_day = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d")
    _shard(tmp_path, old_day, _startup_metric(), pid=2)
    assert h._shards_in_window(tmp_path, 14) == [fresh]


def test_shards_in_window_ignores_unparseable_filenames(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "metrics-short.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "metrics-2026-13-99-1.jsonl").write_text("{}\n", encoding="utf-8")
    assert h._shards_in_window(tmp_path, 14) == []


# --- aggregation guard clauses ----------------------------------------------


def test_pct_from_buckets_returns_zero_when_the_counts_do_not_sum_positive() -> None:
    """A malformed histogram (negative counts) and an empty one both report 0."""
    assert h._pct_from_buckets([0, 0, -1, 0, 0, 0], _BOUNDS, 0.9) == 0.0
    assert h._pct_from_buckets([], [], 0.5) == 0.0


def test_hist_ignores_a_data_point_with_unparseable_bounds() -> None:
    hist = h._Hist()
    hist.add({"count": 3, "explicit_bounds": ["nope"], "bucket_counts": [1]})
    assert hist.stats()["count"] == 0
    assert hist.total_count == 0


def test_hist_treats_an_unparseable_timestamp_as_the_oldest() -> None:
    hist = h._Hist()
    hist.add({"count": 1, "time_unix_nano": "not-a-number", "explicit_bounds": [], "sum": 1.0})
    assert hist.stats()["count"] == 1


def test_hist_ignores_a_bucket_list_that_disagrees_with_the_group_length() -> None:
    """A malformed shard mixing lengths under one bounds list must not IndexError."""
    hist = h._Hist()
    hist.add({"count": 1, "bucket_counts": [1, 0], "explicit_bounds": [10]})
    hist.add({"count": 1, "bucket_counts": [1, 0, 0], "explicit_bounds": [10]})
    assert hist.buckets == [1, 0]
    assert hist.count == 2


def test_hist_adopts_bucket_widths_from_the_first_point_that_has_them() -> None:
    """A bounds group opened by a bucket-less point must still accept later buckets."""
    hist = h._Hist()
    hist.add({"count": 1, "sum": 5.0, "explicit_bounds": [10]})
    assert hist.buckets == []
    hist.add({"count": 1, "sum": 5.0, "bucket_counts": [1, 0], "explicit_bounds": [10]})
    assert hist.buckets == [1, 0]
    assert hist.count == 2


def test_day_of_falls_back_when_the_timestamp_is_out_of_range() -> None:
    assert h._day_of({"time_unix_nano": 10**30}, "2026-07-11") == "2026-07-11"
    assert h._day_of({}, "2026-07-11") == "2026-07-11"


def test_aggregate_skips_bad_json_lines_and_foreign_metric_names(tmp_path: Path) -> None:
    directory = tmp_path / "m"
    shard = _shard(directory, _today(), _startup_metric())
    with shard.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write(
            json.dumps(
                {
                    "resource_metrics": [
                        {"scope_metrics": [{"metrics": [{"name": "otherlib.thing"}]}]}
                    ]
                }
            )
            + "\n"
        )
    result = h._aggregate([shard])
    assert result["startup"]["overall"]["count"] == 2
    assert result["other"] == []


def test_aggregate_skips_an_unreadable_shard(tmp_path: Path) -> None:
    """An OSError on one shard must not abort the whole scan."""
    directory = tmp_path / "m"
    good = _shard(directory, _today(), _startup_metric(), pid=1)
    missing = directory / "metrics-2026-07-11-9.jsonl"
    result = h._aggregate([missing, good])
    assert result["startup"]["overall"]["count"] == 2


# --- _parse_startup_metrics --------------------------------------------------


def test_parse_startup_metrics_empty_window_reports_no_shards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(h, "_telemetry_cfg", lambda: _state(tmp_path / "absent"))
    assert h._parse_startup_metrics() == {
        "startup": None,
        "turn": None,
        "other": [],
        "shard_count": 0,
    }
    assert h._CACHE is None and h._CACHE_KEY is None


def _state(directory: Path) -> h._TelemetryState:
    return h._TelemetryState(
        enabled=True,
        directory=directory,
        env_pinned=False,
        env_var=h.TELEMETRY_ENV_VAR,
        otlp_configured=False,
    )


def test_parse_startup_metrics_caches_on_the_shard_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "m"
    shard = _shard(directory, _today(), _startup_metric())
    monkeypatch.setattr(h, "_telemetry_cfg", lambda: _state(directory))
    first = h._parse_startup_metrics()
    assert first["shard_count"] == 1
    assert first["startup"]["overall"]["count"] == 2
    # A cache HIT hands back the very same object.
    assert h._parse_startup_metrics() is first
    # Appending changes (mtime, size), so the fingerprint no longer matches.
    with shard.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"resource_metrics": []}) + "\n")
    second = h._parse_startup_metrics()
    assert second is not first
    assert second["startup"]["overall"]["count"] == 2


# --- _context_block / _cost_block -------------------------------------------


def test_context_block_returns_none_when_nothing_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(h, "context_occupancy", lambda days: {"turns": []})
    assert h._context_block() is None


def test_context_block_returns_none_when_the_row_store_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_days: int) -> dict:
        raise RuntimeError("row store unreadable")

    monkeypatch.setattr(h, "context_occupancy", _boom)
    assert h._context_block() is None


def test_context_block_passes_the_window_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    def _occupancy(days: int) -> dict:
        seen.append(days)
        return {"turns": [{"slot": "chat-1-1"}]}

    monkeypatch.setattr(h, "context_occupancy", _occupancy)
    assert h._context_block() == {"turns": [{"slot": "chat-1-1"}]}
    assert seen == [h._WINDOW_DAYS]


def test_cost_block_returns_none_when_nothing_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(h, "cost_breakdown", lambda days: {"turns": []})
    assert h._cost_block() is None


def test_cost_block_returns_none_when_the_row_store_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_days: int) -> dict:
        raise RuntimeError("row store unreadable")

    monkeypatch.setattr(h, "cost_breakdown", _boom)
    assert h._cost_block() is None


def test_cost_block_uses_the_shorter_spend_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    def _cost(days: int) -> dict:
        seen.append(days)
        return {"turns": [{"slot": "chat-1-1"}]}

    monkeypatch.setattr(h, "cost_breakdown", _cost)
    assert h._cost_block()
    assert seen == [h._COST_WINDOW_DAYS]


# --- GET /api/telemetry/startup ---------------------------------------------


@pytest.mark.asyncio
async def test_startup_route_reports_posture_and_all_four_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(h, "_telemetry_cfg", lambda: _state(tmp_path / "m"))
    monkeypatch.setattr(
        h,
        "_parse_startup_metrics",
        lambda: {
            "startup": {"overall": {"count": 1}},
            "turn": {"count": 1},
            "other": [{"name": "kirocrew.x", "kind": "counter"}],
            "shard_count": 3,
        },
    )
    monkeypatch.setattr(h, "_context_block", lambda: {"turns": [1]})
    monkeypatch.setattr(h, "_cost_block", lambda: None)
    payload = _body(await h.api_telemetry_startup(_mk()))
    assert payload["enabled"] is True
    assert payload["window_days"] == h._WINDOW_DAYS
    assert payload["metrics_dir"] == str(tmp_path / "m")
    assert payload["shard_count"] == 3
    assert payload["startup"] == {"overall": {"count": 1}}
    assert payload["turn"] == {"count": 1}
    assert payload["context"] == {"turns": [1]}
    assert payload["cost"] is None
    assert payload["other"][0]["name"] == "kirocrew.x"


@pytest.mark.asyncio
async def test_startup_route_titles_the_cost_ranking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(h, "_telemetry_cfg", lambda: _state(tmp_path / "m"))
    monkeypatch.setattr(
        h, "_parse_startup_metrics", lambda: {"startup": None, "turn": None, "other": []}
    )
    monkeypatch.setattr(h, "_context_block", lambda: None)
    monkeypatch.setattr(
        h, "_cost_block", lambda: {"turns": [1], "conversations": [{"slot": "chat-1-9"}]}
    )
    slot = SimpleNamespace(display_title="zzq ranked convo")
    state = SimpleNamespace(get_slot=lambda key: slot, conversation_log=None)
    payload = _body(await h.api_telemetry_startup(_mk(state=state)))
    assert payload["cost"]["conversations"] == [{"slot": "chat-1-9", "title": "zzq ranked convo"}]
    assert payload["shard_count"] == 0


# --- GET /api/telemetry/context-trace ---------------------------------------


@pytest.mark.asyncio
async def test_context_trace_400_without_a_slot() -> None:
    response = await h.api_context_trace(_mk("/api/telemetry/context-trace", query="?slot=%20"))
    assert response.status == 400
    assert _body(response)["code"] == "slot_required"


@pytest.mark.asyncio
async def test_context_trace_returns_the_per_turn_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, int]] = []

    def _trace(slot: str, days: int) -> dict:
        seen.append((slot, days))
        return {"turns": [{"blocks": []}]}

    monkeypatch.setattr(h, "context_trace", _trace)
    request = _mk("/api/telemetry/context-trace", query="?slot=chat-1-9")
    assert _body(await h.api_context_trace(request)) == {"turns": [{"blocks": []}]}
    assert seen == [("chat-1-9", h._WINDOW_DAYS)]


# --- _persisted_titles -------------------------------------------------------


def test_persisted_titles_reads_each_transcript_once() -> None:
    log = MagicMock()
    log.get_metadata = MagicMock(return_value={"title": "zzq persisted"})
    out = h._persisted_titles(log, ["chat-1-9", "chat-1-9", "chat-2-4"])
    assert out == {"chat-1-9": "zzq persisted", "chat-2-4": "zzq persisted"}
    # Deduplicated by TRANSCRIPT key, so the repeated slot costs no second read.
    assert log.get_metadata.call_count == 2


def test_persisted_titles_drops_the_placeholder_and_the_unnamed() -> None:
    log = MagicMock()
    log.get_metadata = MagicMock(side_effect=[{"title": NEW_SESSION_TITLE}, {}])
    assert h._persisted_titles(log, ["chat-1-9", "chat-2-4"]) == {}


def test_persisted_titles_survives_an_unreadable_metadata_line() -> None:
    log = MagicMock()
    log.get_metadata = MagicMock(side_effect=OSError("truncated"))
    assert h._persisted_titles(log, ["chat-1-9"]) == {}


# --- _with_conversation_titles ----------------------------------------------


@pytest.mark.asyncio
async def test_with_titles_is_a_noop_without_dashboard_state() -> None:
    cost = {"conversations": [{"slot": "chat-1-9"}]}
    assert await h._with_conversation_titles(_mk(), cost) is cost


@pytest.mark.asyncio
async def test_with_titles_is_a_noop_when_state_exposes_no_slot_accessor() -> None:
    """``get_slot`` is the only public way in — a ``slots`` attribute is not it."""
    cost = {"conversations": [{"slot": "chat-1-9"}]}
    state = SimpleNamespace(_slots={"chat-1-9": object()})
    assert await h._with_conversation_titles(_mk(state=state), cost) is cost


@pytest.mark.asyncio
async def test_with_titles_prefers_the_live_slot_and_redacts_it() -> None:
    """This is a second serialization boundary for an LLM-authored field."""
    slot = SimpleNamespace(display_title="key AKIAIOSFODNN7EXAMPLE leaked")
    state = SimpleNamespace(get_slot=lambda key: slot, conversation_log=None)
    cost: dict[str, Any] = {
        "turns": [1],
        "conversations": [{"slot": "chat-1-9", "cost_usd": 0.5}],
    }
    out = await h._with_conversation_titles(_mk(state=state), cost)
    title = out["conversations"][0]["title"]
    assert "AKIAIOSFODNN7EXAMPLE" not in title
    assert "REDACTED" in title
    # The memoised cost object must not gain the title by reference.
    assert "title" not in cost["conversations"][0]
    assert out["turns"] == [1]


@pytest.mark.asyncio
async def test_with_titles_falls_back_to_the_persisted_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed conversation has no slot, so its name comes off the metadata line."""
    log = MagicMock()
    log.get_metadata = MagicMock(return_value={"title": "zzq closed convo"})
    state = SimpleNamespace(get_slot=lambda key: None, conversation_log=log)
    cost = {"conversations": [{"slot": "chat-1-9"}, {"slot": ""}]}
    out = await h._with_conversation_titles(_mk(state=state), cost)
    assert out["conversations"][0]["title"] == "zzq closed convo"
    assert "title" not in out["conversations"][1]


@pytest.mark.asyncio
async def test_with_titles_ignores_a_placeholder_slot_title() -> None:
    slot = SimpleNamespace(display_title=NEW_SESSION_TITLE)
    state = SimpleNamespace(get_slot=lambda key: slot, conversation_log=None)
    cost = {"conversations": [{"slot": "chat-1-9"}]}
    out = await h._with_conversation_titles(_mk(state=state), cost)
    assert "title" not in out["conversations"][0]


# --- _telemetry_overlay_pins -------------------------------------------------


def test_overlay_pins_false_when_no_overlay_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config_loader, "config_local_path", lambda: tmp_path / "config.local.json")
    assert h._telemetry_overlay_pins("enabled") is False


def test_overlay_pins_true_only_for_the_named_leaf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = tmp_path / "config.local.json"
    overlay.write_text(json.dumps({"telemetry": {"enabled": False}}), encoding="utf-8")
    monkeypatch.setattr(config_loader, "config_local_path", lambda: overlay)
    assert h._telemetry_overlay_pins("enabled") is True
    assert h._telemetry_overlay_pins("beacon_enabled") is False


def test_overlay_pins_false_on_a_malformed_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = tmp_path / "config.local.json"
    overlay.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(config_loader, "config_local_path", lambda: overlay)
    assert h._telemetry_overlay_pins("enabled") is False


def test_overlay_pins_false_when_the_overlay_is_not_an_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = tmp_path / "config.local.json"
    overlay.write_text(json.dumps(["telemetry"]), encoding="utf-8")
    monkeypatch.setattr(config_loader, "config_local_path", lambda: overlay)
    assert h._telemetry_overlay_pins("enabled") is False


# --- GET /api/telemetry/beacon ----------------------------------------------


def _beacon_stub(monkeypatch: pytest.MonkeyPatch, info: dict, *, env_opted_out: bool) -> MagicMock:
    stub = MagicMock()
    stub.status = MagicMock(return_value=info)
    stub.is_env_opted_out = MagicMock(return_value=env_opted_out)
    stub.DISABLE_ENV = "KIROCREW_TELEMETRY_DISABLED"
    monkeypatch.setattr(h, "beacon", stub)
    return stub


@pytest.mark.asyncio
async def test_beacon_status_reports_stored_flag_and_effective_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, _cfg_stub(beacon_enabled=True))
    monkeypatch.setattr(h, "_telemetry_overlay_pins", lambda leaf: leaf == "beacon_enabled")
    stub = _beacon_stub(
        monkeypatch,
        {
            "beacon_enabled": True,
            "would_send": False,
            "reason": "ci host",
            "reason_code": "ci",
            "endpoint_configured": True,
            "governance_pinned_off": True,
        },
        env_opted_out=True,
    )
    payload = _body(await h.api_beacon_status(_mk("/api/telemetry/beacon")))
    assert payload["enabled"] is True
    # A stored "on" beside an effective "no" is the whole point of this route.
    assert payload["would_send"] is False
    assert payload["reason_code"] == "ci"
    assert payload["endpoint_configured"] is True
    assert payload["env_override"] is True
    assert payload["env_var"] == "KIROCREW_TELEMETRY_DISABLED"
    assert payload["overlay_override"] is True
    assert payload["governance_override"] is True
    assert stub.status.call_args.args[0] == "https://beacon.invalid/x"
    assert stub.status.call_args.kwargs["acked"] is True


@pytest.mark.asyncio
async def test_beacon_status_never_500s_on_an_unreadable_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A diagnostic must render exactly when the config is broken — failing to off."""
    loader = MagicMock()
    loader.load = MagicMock(side_effect=RuntimeError("unreadable"))
    monkeypatch.setattr(h, "KiroCrewConfig", loader)
    stub = _beacon_stub(
        monkeypatch, {"would_send": False, "reason_code": "no_endpoint"}, env_opted_out=False
    )
    payload = _body(await h.api_beacon_status(_mk("/api/telemetry/beacon")))
    assert payload["enabled"] is False
    assert payload["would_send"] is False
    assert payload["overlay_override"] is False
    assert payload["governance_override"] is False
    assert stub.status.call_args.args[0] == ""


# --- GET /api/telemetry/collection ------------------------------------------


@pytest.mark.asyncio
async def test_collection_status_reports_the_effective_switch_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = h._TelemetryState(
        enabled=True,
        directory=tmp_path / "m",
        env_pinned=True,
        env_var=h.TELEMETRY_ENV_VAR,
        otlp_configured=True,
    )
    monkeypatch.setattr(h, "_telemetry_cfg", lambda: state)
    monkeypatch.setattr(h, "_telemetry_overlay_pins", lambda leaf: False)
    payload = _body(await h.api_collection_status(_mk("/api/telemetry/collection")))
    assert payload == {
        "enabled": True,
        "env_pinned": True,
        "env_var": h.TELEMETRY_ENV_VAR,
        "overlay_override": False,
        "otlp_configured": True,
        "metrics_dir": str(tmp_path / "m"),
    }
