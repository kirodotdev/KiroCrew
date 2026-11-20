"""The exploratory tip bank, its read route, and its feedback and metric path."""

from __future__ import annotations

import asyncio
import json
import os
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from kiro_crew.metrics import feature_usage as fu
from kiro_crew.tips import (
    TipsCache,
    TipsState,
    _load_curated_tips,
    api_tips_exploratory,
    api_tips_feedback,
    get_tips_cache,
)
from kiro_crew.tips_exploratory import (
    REQUIREMENT_KEYS,
    _clean,
    exploratory_tip_ids,
    load_exploratory_tips,
)


def _bank_ids() -> list[str]:
    """Every id the packaged bank file lists, read from the file itself."""
    from kiro_crew import tips_exploratory

    data = json.loads(tips_exploratory._FILE.read_text(encoding="utf-8"))
    return [entry["id"] for entry in data["tips"]]


# -- the bank -----------------------------------------------------------------


def test_every_shipped_entry_loads_and_is_well_formed() -> None:
    # Nothing in the packaged file is dropped by validation: a bad entry there
    # would silently never show.
    tips = load_exploratory_tips()
    assert tips
    assert [t["id"] for t in tips] == _bank_ids()
    for t in tips:
        assert set(t["requires"]) <= REQUIREMENT_KEYS  # type: ignore[arg-type]
        assert str(t["source"]).startswith("https://")


def test_exploratory_ids_never_collide_with_curated_ids() -> None:
    # Dismissals share one store, so a shared id would dismiss a card tip too.
    assert not exploratory_tip_ids() & {t["id"] for t in _load_curated_tips()}


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "Bad Id", "body": "b", "requires": [], "source": "https://x"},
        {"id": "ok", "body": "", "requires": [], "source": "https://x"},
        {"id": "ok", "body": "two\nlines", "requires": [], "source": "https://x"},
        {"id": "ok", "body": "b", "requires": ["no-such-key"], "source": "https://x"},
        {"id": "ok", "body": "b", "requires": [], "source": "http://x"},
        "not-a-dict",
    ],
)
def test_a_malformed_entry_is_dropped(entry: Any) -> None:
    assert _clean(entry) is None


# -- GET /api/tips/exploratory --------------------------------------------------


def _cache(**state: Any) -> TipsCache:
    cache = TipsCache()
    cache.curated = [{"id": "split-view", "doc": "", "doc_link": ""}]
    cache.exploratory = [dict(t) for t in load_exploratory_tips()]
    cache.state = TipsState(**state)
    return cache


async def _get(cache: TipsCache, tmp_path: Path, tips_enabled: bool = True) -> dict[str, Any]:
    from aiohttp.test_utils import make_mocked_request

    cfg = MagicMock()
    cfg.dashboard.tips_enabled = tips_enabled
    with (
        patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}),
        patch("kiro_crew.tips.KiroCrewConfig") as mock_cfg_cls,
    ):
        mock_cfg_cls.load.return_value = cfg
        req = make_mocked_request("GET", "/api/tips/exploratory")
        req.app["state"] = types.SimpleNamespace(_tips_cache=cache)
        resp = await api_tips_exploratory(req)
    assert resp.status == 200
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_the_route_lists_the_bank_without_dismissed_tips(tmp_path: Path) -> None:
    first = _bank_ids()[0]
    body = await _get(_cache(dismissed=[first]), tmp_path)
    ids = [t["id"] for t in body["tips"]]
    assert first not in ids
    assert ids == [i for i in _bank_ids() if i != first]
    assert set(body["tips"][0]) == {"id", "body", "requires"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "tips_enabled"),
    [({}, False), ({"opted_out": True}, True), ({"load_failed": True}, True)],
)
async def test_the_tips_switch_hides_every_exploratory_tip(
    tmp_path: Path, state: dict[str, Any], tips_enabled: bool
) -> None:
    body = await _get(_cache(**state), tmp_path, tips_enabled=tips_enabled)
    assert body == {"tips": []}


@pytest.mark.asyncio
async def test_the_public_pool_cache_loads_the_bank(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
        cache = await get_tips_cache(types.SimpleNamespace())  # type: ignore[arg-type]
    assert [t["id"] for t in cache.exploratory] == _bank_ids()


# -- POST /api/tips/feedback with an exploratory id ----------------------------


def _feedback_request(state: Any, body: dict[str, Any]):
    from aiohttp import streams
    from aiohttp.test_utils import make_mocked_request

    payload = streams.StreamReader(
        Mock(_reading_paused=False), 2**16, loop=asyncio.get_event_loop()
    )
    payload.feed_data(json.dumps(body).encode())
    payload.feed_eof()
    req = make_mocked_request("POST", "/api/tips/feedback", payload=payload)
    req.app["state"] = state
    return req


class _Rec:
    enabled = True

    def __init__(self) -> None:
        self.counters: list[dict[str, Any]] = []

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.counters.append({"name": name, "attrs": dict(attrs or {})})

    def histogram(self, *a, **k) -> None:  # type: ignore[no-untyped-def]
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["shown", "dismiss"])
async def test_exploratory_feedback_leaves_the_card_tip_alone(tmp_path: Path, action: str) -> None:
    tip_id = _bank_ids()[0]
    offered = {"id": "split-view", "doc": ""}
    cache = _cache(offered=dict(offered), last_shown_ts=123.0)
    rec = _Rec()
    with (
        patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}),
        patch("kiro_crew.metrics.provider.get_recorder", return_value=rec),
    ):
        resp = await api_tips_feedback(
            _feedback_request(
                types.SimpleNamespace(_tips_cache=cache), {"id": tip_id, "action": action}
            )
        )
    assert resp.status == 200
    st = cache.state
    # The card's offered tip and cadence are untouched by a thinking-row tip.
    assert st.offered == offered
    assert st.last_shown_ts == 123.0
    assert (tip_id in st.dismissed) is (action == "dismiss")
    assert "split-view" not in st.dismissed
    [point] = [c for c in rec.counters if c["name"] == fu.TIPS_FEEDBACK]
    assert point["attrs"] == {
        "action": action,
        "source": "exploratory",
        "feature": tip_id,
    }


def test_an_exploratory_id_is_kept_as_the_feature() -> None:
    for tip_id in _bank_ids():
        assert fu.tip_feature_label(tip_id) == tip_id


@pytest.mark.asyncio
async def test_an_unsaved_exploratory_dismissal_is_not_kept(tmp_path: Path) -> None:
    tip_id = _bank_ids()[0]
    cache = _cache()
    with (
        patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}),
        patch("kiro_crew.tips._save_state", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        await api_tips_feedback(
            _feedback_request(
                types.SimpleNamespace(_tips_cache=cache), {"id": tip_id, "action": "dismiss"}
            )
        )
    assert tip_id not in cache.state.dismissed
