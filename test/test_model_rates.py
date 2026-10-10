"""A model's credit multiplier changing between catalog reads posts one bell note.

A repricing such as ``gpt-5.6-sol`` going from 2.4x to 4.4x must not pass
silently. These tests pin the comparison (first sighting is silent, a change is
reported, an absent row keeps its baseline, an unpriced row is ignored) and the
wiring (a fresh catalog fetch pushes one ``system.models`` note through the bus).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import model_rates
from kiro_crew.model_rates import RateChange, observe_catalog_rates
from kiro_crew.notifications.bus import SYSTEM_CHANNELS


def _row(name: str, rate: object, **extra: object) -> dict:
    return {"model_name": name, "rate_multiplier": rate, **extra}


def test_first_sighting_records_baseline_and_reports_nothing(tmp_path: Path) -> None:
    path = tmp_path / "model_rates.json"
    assert observe_catalog_rates([_row("gpt-5.6-sol", 2.4)], path) == []
    assert json.loads(path.read_text()) == {"gpt-5.6-sol": 2.4}


def test_changed_multiplier_is_reported_once(tmp_path: Path) -> None:
    path = tmp_path / "model_rates.json"
    observe_catalog_rates([_row("gpt-5.6-sol", 2.4), _row("claude-opus-5", 2.2)], path)
    changes = observe_catalog_rates([_row("gpt-5.6-sol", 4.4), _row("claude-opus-5", 2.2)], path)
    assert changes == [RateChange("gpt-5.6-sol", 2.4, 4.4)]
    # The new rate is now the baseline: the next identical read is silent.
    assert observe_catalog_rates([_row("gpt-5.6-sol", 4.4)], path) == []


def test_absent_row_keeps_its_baseline(tmp_path: Path) -> None:
    path = tmp_path / "model_rates.json"
    observe_catalog_rates([_row("a", 1.0), _row("b", 2.0)], path)
    assert observe_catalog_rates([_row("a", 1.0)], path) == []
    assert observe_catalog_rates([_row("b", 3.0)], path) == [RateChange("b", 2.0, 3.0)]


@pytest.mark.parametrize("bad", [None, 0, -1, float("nan"), float("inf"), True, "2.4"])
def test_unpriced_rows_neither_record_nor_report(tmp_path: Path, bad: object) -> None:
    path = tmp_path / "model_rates.json"
    observe_catalog_rates([_row("m", 2.0)], path)
    assert observe_catalog_rates([_row("m", bad)], path) == []
    assert json.loads(path.read_text()) == {"m": 2.0}


def test_model_id_wins_over_printed_name(tmp_path: Path) -> None:
    path = tmp_path / "model_rates.json"
    observe_catalog_rates([_row("Pretty Name", 1.0, model_id="real-id")], path)
    assert json.loads(path.read_text()) == {"real-id": 1.0}


def test_float_noise_is_not_a_change(tmp_path: Path) -> None:
    path = tmp_path / "model_rates.json"
    observe_catalog_rates([_row("m", 2.4)], path)
    assert observe_catalog_rates([_row("m", 2.4000000001)], path) == []


def test_corrupt_sidecar_reads_as_no_baseline(tmp_path: Path) -> None:
    path = tmp_path / "model_rates.json"
    path.write_text("{not json")
    assert observe_catalog_rates([_row("m", 2.0)], path) == []
    assert json.loads(path.read_text()) == {"m": 2.0}


def test_unrecorded_change_is_not_reported(tmp_path: Path) -> None:
    path = tmp_path / "model_rates.json"
    observe_catalog_rates([_row("m", 1.0)], path)

    def _fail(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    with patch.object(model_rates, "atomic_write", _fail):
        assert observe_catalog_rates([_row("m", 2.0)], path) == []
    # The baseline still holds the old rate, so the change is reported once written.
    assert observe_catalog_rates([_row("m", 2.0)], path) == [RateChange("m", 1.0, 2.0)]


def test_note_uses_the_picker_spelling() -> None:
    title, body = model_rates.change_note([RateChange("gpt-5.6-sol", 2.4, 4.4)])
    assert title == "Model credit rate changed"
    assert body == "gpt-5.6-sol now bills at 4.4x credits (was 2.4x)."
    assert [model_rates.format_multiplier(v) for v in (1, 2.2, 0.25, 0.05)] == [
        "1.0x",
        "2.2x",
        "0.25x",
        "0.05x",
    ]


def test_models_channel_is_registered() -> None:
    assert "system.models" in SYSTEM_CHANNELS


def test_catalog_fetch_pushes_one_note_on_a_repricing(tmp_path: Path) -> None:
    from kiro_crew.dashboard.handlers import agents

    path = tmp_path / "model_rates.json"
    path.write_text(json.dumps({"gpt-5.6-sol": 2.4}))
    bus = MagicMock()

    async def _fetch() -> list[dict]:
        return [_row("gpt-5.6-sol", 4.4)]

    async def _go() -> list[dict]:
        with (
            patch.object(agents, "_fetch_kiro_catalog", _fetch),
            patch.object(model_rates, "sidecar_path", lambda: path),
        ):
            return await agents._shared_catalog_fetch(bus)

    saved = (agents._catalog_cache.models, agents._catalog_cache.fetched_at)
    try:
        rows = asyncio.run(_go())
    finally:
        agents._catalog_cache.models, agents._catalog_cache.fetched_at = saved
        agents._catalog_cache.task = None
    assert rows == [_row("gpt-5.6-sol", 4.4)]
    bus.push.assert_called_once()
    payload = bus.push.call_args.args[0]
    assert payload.channel == "system.models"
    assert payload.body == "gpt-5.6-sol now bills at 4.4x credits (was 2.4x)."


def test_bus_failure_never_costs_the_catalog(tmp_path: Path) -> None:
    from kiro_crew.dashboard.handlers import agents

    path = tmp_path / "model_rates.json"
    path.write_text(json.dumps({"m": 1.0}))
    bus = MagicMock()
    bus.push.side_effect = RuntimeError("feed down")

    async def _fetch() -> list[dict]:
        return [_row("m", 2.0)]

    async def _go() -> list[dict]:
        with (
            patch.object(agents, "_fetch_kiro_catalog", _fetch),
            patch.object(model_rates, "sidecar_path", lambda: path),
        ):
            return await agents._shared_catalog_fetch(bus)

    saved = (agents._catalog_cache.models, agents._catalog_cache.fetched_at)
    try:
        assert asyncio.run(_go()) == [_row("m", 2.0)]
    finally:
        agents._catalog_cache.models, agents._catalog_cache.fetched_at = saved
        agents._catalog_cache.task = None
