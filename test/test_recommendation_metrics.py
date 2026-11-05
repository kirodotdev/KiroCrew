"""Recommendation metrics: tips, the Explore panel and feature videos.

Three questions these counters answer, and the tests that hold each one closed:

* is a recommendation used -- ``kirocrew.tips.feedback`` and
  ``kirocrew.feature_video.status``, each with closed value lists;
* does the Explore panel get used -- the existing ``kirocrew.milestone.*``
  counters, now exported, with every attribute value checked at the emit site;
* does a recommended feature get used -- ``recommended_by`` on
  ``kirocrew.milestone.reached``, read from a local, windowed record that holds
  only catalog ids, channel names and timestamps.

The route tests drive the REAL handlers (tips feedback, the feature-video status
write) and the real milestones state file, not a mocked call site.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from kiro_crew import milestones as m
from kiro_crew.metrics import feature_usage as fu
from kiro_crew.testing.wait import async_wait_until
from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

_REAL_CONSENT = m._recommendation_consent


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Path:
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _floor_monkeypatch.setattr(m, "_catalog_cache", None)
    _floor_monkeypatch.setattr(m, "_policy_permits", lambda: True)
    _floor_monkeypatch.setattr(m, "_probe", lambda _signal: False)
    # Telemetry collection on, so recommendations are stamped; the consent
    # tests below restore the real check.
    _floor_monkeypatch.setattr(m, "_recommendation_consent", lambda: True)
    return tmp_path


@pytest.fixture
def metrics(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(m, "emit_counter", lambda name, attrs: seen.append((name, dict(attrs))))
    return seen


class _Rec:
    enabled = True

    def __init__(self) -> None:
        self.counters: list[dict[str, Any]] = []

    def counter(self, name, value=1, *, attrs=None, **kwargs) -> None:
        self.counters.append({"name": name, "attrs": dict(attrs or {})})

    def histogram(self, *a, **k) -> None:
        pass


@pytest.fixture
def rec():
    r = _Rec()
    with patch("kiro_crew.metrics.provider.get_recorder", return_value=r):
        yield r


def _reached(metrics: list, milestone: str) -> list[dict[str, Any]]:
    return [a for n, a in metrics if n == m.M_REACHED and a["milestone"] == milestone]


# -- closed value lists -------------------------------------------------------


def test_value_lists_are_pinned():
    assert fu.TIP_ACTION_VALUES == (
        "shown",
        "ack",
        "dismiss",
        "snooze",
        "helpful",
        "optout",
        "optin",
        "other",
    )
    assert fu.TIP_SOURCE_VALUES == ("curated", "catalog", "generated", "other")
    assert fu.VIDEO_STATUS_VALUES == ("seen", "dismissed", "other")
    assert m.RECOMMENDATION_CHANNELS == ("tip", "explore", "video")
    assert m.RECOMMENDED_BY_VALUES == ("tip", "explore", "video", "several", "none")
    assert m.RECOMMENDATION_WINDOW_S == 14 * 24 * 3600


def test_tip_actions_match_the_route():
    import inspect

    from kiro_crew import tips

    src = inspect.getsource(tips.api_tips_feedback)
    for action in fu.TIP_ACTION_VALUES[:-1]:
        assert f'"{action}"' in src, action


def test_video_statuses_match_the_store():
    from kiro_crew.feature_videos import VALID_STATUSES

    assert fu.VIDEO_STATUS_VALUES == (*VALID_STATUSES, "other")


def test_explore_feature_ids_are_the_live_feature_rows():
    assert fu.EXPLORE_FEATURE_IDS == tuple(r.id for r in m.feature_rows())


def test_milestone_attribute_lists_are_pinned():
    assert m._ATTR_VALUES == {
        "group": ("features", "sessions", "turns", "streak", "concurrent"),
        "surface": ("empty_state", "panel"),
        "send": ("prefill", "send", "route"),
        "via": ("starter", "organic", "backfill", "manual", "arrived", "mixed"),
        "recommended_by": ("tip", "explore", "video", "several", "none"),
    }
    assert m._milestone_keys() == frozenset(
        {
            "all",
            "widgets",
            "scheduled-task",
            "mate",
            "parallel-chats",
            "slack",
            "pr-watch",
            "crew-mode",
            "chats-opened:10",
            "chats-opened:100",
            "chats-opened:1000",
            "turns-sent:100",
            "turns-sent:1000",
            "turns-sent:10000",
            "streak-best:3",
            "streak-best:7",
            "streak-best:30",
            "streak-best:100",
        }
    )


def test_the_route_surfaces_are_the_closed_surface_list():
    from kiro_crew.dashboard import milestones_api

    assert milestones_api._SURFACES == m._ATTR_VALUES["surface"]


def test_an_open_value_is_sent_as_other(metrics: list):
    m.apply_now(m.op_skip("widgets", "a-surface-nobody-validated"))
    m.apply_now(m.op_skip("not-a-row", "panel"))
    skipped = [a for n, a in metrics if n == m.M_SKIPPED]
    assert skipped == [
        {"surface": "other", "milestone": "widgets"},
        {"surface": "panel", "milestone": "other"},
    ]
    assert m.closed_attrs({"unknown_key": "x", "send": 3}) == {
        "unknown_key": "other",
        "send": "other",
    }


def test_a_metric_error_does_not_fail_the_write(monkeypatch: pytest.MonkeyPatch):
    def boom(*_a, **_k):
        raise RuntimeError("recorder down")

    monkeypatch.setattr(m, "closed_attrs", boom)
    m.apply_now(m.op_mark_done("slack"))
    assert m.load()["reached"]["slack"]["source"] == "manual"


# -- the attribution table ----------------------------------------------------


def test_recommendation_targets_are_pinned():
    assert m.RECOMMENDATION_TARGETS == {
        "artifacts.md": ("widgets",),
        "cron-and-scheduling.md": ("scheduled-task",),
        "crew-members.md": ("mate", "crew-mode"),
        "subagents.md": ("parallel-chats",),
        "slack-integration.md": ("slack",),
        "monitor-loops.md": ("pr-watch",),
        "monitor-loops": ("pr-watch",),
    }


def test_every_target_is_a_feature_row_and_every_row_is_reachable():
    rows = {r.id: r for r in m.feature_rows()}
    for key, targets in m.RECOMMENDATION_TARGETS.items():
        assert targets and set(targets) <= set(rows), key
        assert fu.tip_feature_label(key) == key or fu.video_feature_label(key) == key, key
    # Each feature row's own doc maps back to it, so a tip about that doc counts.
    for row in rows.values():
        assert row.id in m.RECOMMENDATION_TARGETS.get(row.doc, ()), row.id


def test_the_video_keys_are_real_video_features():
    from kiro_crew.feature_videos import CATALOG

    video_features = {e.feature for e in CATALOG}
    keys = {k for k in m.RECOMMENDATION_TARGETS if not k.endswith(".md")}
    assert keys <= video_features
    for entry in CATALOG:
        assert fu.video_feature_label(entry.feature) == entry.feature


# -- labels -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("doc", "label"),
    [
        ("monitor-loops.md", "monitor-loops.md"),
        ("my-notes.md", "other"),
        ("", "other"),
        (3, "other"),
    ],
)
def test_tip_feature_label(doc, label):
    assert fu.tip_feature_label(doc) == label


@pytest.mark.parametrize(
    ("feature", "label"),
    [
        ("monitor-loops", "monitor-loops"),
        ("feature-tips", "feature-tips"),
        ("pr-watch", "pr-watch"),
        ("skills.md", "skills.md"),
        ("publisher-chose-this", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_video_feature_label(feature, label):
    assert fu.video_feature_label(feature) == label


def test_every_tip_feature_label_is_an_allowlisted_doc():
    for doc in TIP_DOC_ALLOWLIST:
        assert fu.tip_feature_label(doc) == doc


def test_record_functions_close_every_value(rec):
    fu.record_tip_feedback("shown", "catalog", "monitor-loops.md")
    fu.record_tip_feedback("bogus", "llm", "/etc/passwd")
    fu.record_feature_video_status("seen", "monitor-loops")
    fu.record_feature_video_status("played", "secret-feature")
    assert rec.counters == [
        {
            "name": fu.TIPS_FEEDBACK,
            "attrs": {"action": "shown", "source": "catalog", "feature": "monitor-loops.md"},
        },
        {
            "name": fu.TIPS_FEEDBACK,
            "attrs": {"action": "other", "source": "other", "feature": "other"},
        },
        {
            "name": fu.FEATURE_VIDEO_STATUS,
            "attrs": {"status": "seen", "feature": "monitor-loops"},
        },
        {
            "name": fu.FEATURE_VIDEO_STATUS,
            "attrs": {"status": "other", "feature": "other"},
        },
    ]


def test_record_functions_never_raise():
    with patch("kiro_crew.metrics.feature_usage._emit", side_effect=RuntimeError("x")):
        fu.record_tip_feedback("shown", "catalog", "monitor-loops.md")
        fu.record_feature_video_status("seen", "monitor-loops")


# -- attribution --------------------------------------------------------------


class TestAttribution:
    def test_a_tip_then_first_use_reads_tip_and_clears_the_record(self, metrics: list) -> None:
        m.record_recommended("tip", "monitor-loops.md")
        assert set(m.load()["recommended"]) == {"pr-watch"}
        m.record("pr-watch")
        [attrs] = _reached(metrics, "pr-watch")
        assert attrs["recommended_by"] == "tip"
        assert attrs["via"] == "organic"
        assert m.load()["recommended"] == {}

    def test_two_channels_read_several(self, metrics: list) -> None:
        m.record_recommended("tip", "monitor-loops.md")
        m.record_recommended("video", "monitor-loops")
        m.record("pr-watch")
        assert _reached(metrics, "pr-watch")[0]["recommended_by"] == "several"

    def test_an_explore_click_reads_explore(self, metrics: list) -> None:
        m.apply_now(m.op_starter_clicked("scheduled-task", "panel"))
        m.record("scheduled-task")
        [attrs] = _reached(metrics, "scheduled-task")
        assert attrs == {
            "group": "features",
            "milestone": "scheduled-task",
            "via": "starter",
            "recommended_by": "explore",
        }

    def test_no_recommendation_reads_none(self, metrics: list) -> None:
        m.record("widgets")
        assert _reached(metrics, "widgets")[0]["recommended_by"] == "none"

    def test_a_recommendation_outside_the_window_reads_none(
        self, metrics: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.testing.clock import ManualClock

        clock = ManualClock()
        clock.install(monkeypatch, m)
        m.record_recommended("tip", "slack-integration.md")
        clock.advance(m.RECOMMENDATION_WINDOW_S + 1)
        m.record("slack")
        assert _reached(metrics, "slack")[0]["recommended_by"] == "none"

    def test_inside_the_window_it_still_counts(
        self, metrics: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.testing.clock import ManualClock

        clock = ManualClock()
        clock.install(monkeypatch, m)
        m.record_recommended("tip", "slack-integration.md")
        clock.advance(m.RECOMMENDATION_WINDOW_S - 1)
        m.record("slack")
        assert _reached(metrics, "slack")[0]["recommended_by"] == "tip"

    def test_expired_stamps_are_pruned_on_the_next_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.testing.clock import ManualClock

        clock = ManualClock()
        clock.install(monkeypatch, m)
        m.record_recommended("tip", "slack-integration.md")
        clock.advance(m.RECOMMENDATION_WINDOW_S + 1)
        m.record_recommended("tip", "artifacts.md")
        assert set(m.load()["recommended"]) == {"widgets"}

    def test_an_unmapped_key_writes_nothing(self, metrics: list) -> None:
        m.record_recommended("tip", "skills.md")
        m.record_recommended("video", "feature-tips")
        m.record_recommended("tip", "not-a-doc")
        m.record_recommended("bogus-channel", "artifacts.md")
        assert not m._path().exists() or m.load()["recommended"] == {}

    def test_one_doc_recommends_every_row_it_documents(self) -> None:
        m.record_recommended("tip", "crew-members.md")
        assert set(m.load()["recommended"]) == {"mate", "crew-mode"}

    def test_a_done_feature_is_not_recommended(self) -> None:
        m.record("widgets")
        m.record_recommended("tip", "artifacts.md")
        assert m.load()["recommended"] == {}

    def test_a_manual_mark_reads_none_and_clears(self, metrics: list) -> None:
        m.record_recommended("tip", "slack-integration.md")
        m.apply_now(m.op_mark_done("slack"))
        assert _reached(metrics, "slack")[0] == {
            "group": "features",
            "milestone": "slack",
            "via": "manual",
            "recommended_by": "none",
        }
        assert m.load()["recommended"] == {}

    def test_a_backfill_reads_none(self, metrics: list, monkeypatch: pytest.MonkeyPatch) -> None:
        m.apply_now(m.op_recommended("tip", "artifacts.md"))
        monkeypatch.setattr(m, "_probe", lambda signal: signal == "artifacts_nonempty")
        m.ensure_backfilled()
        [attrs] = _reached(metrics, "widgets")
        assert attrs["via"] == "backfill"
        assert attrs["recommended_by"] == "none"

    def test_usage_rows_always_read_none(self, metrics: list) -> None:
        m.apply_now(m.op_recommended("tip", "artifacts.md"))
        for _ in range(10):
            m.record("chats-opened")
        assert _reached(metrics, "chats-opened:10")[0]["recommended_by"] == "none"
        # A usage reach does not consume a feature's record.
        assert set(m.load()["recommended"]) == {"widgets"}

    def test_the_record_holds_only_ids_channels_and_numbers(self) -> None:
        m.record_recommended("tip", "crew-members.md")
        m.record_recommended("video", "monitor-loops")
        path = m._path()
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        recommended = json.loads(path.read_text())["recommended"]
        assert set(recommended) <= set(fu.EXPLORE_FEATURE_IDS)
        for row in recommended.values():
            assert set(row) <= set(m.RECOMMENDATION_CHANNELS)
            assert all(isinstance(v, float) for v in row.values())

    def test_a_hand_edited_record_degrades(self) -> None:
        m._path().write_text(
            json.dumps(
                {
                    "recommended": {
                        "widgets": {"tip": "yesterday", "email": 5, "video": 10.0},
                        "Not An Id": {"tip": 1.0},
                        "slack": [],
                        "x" * 80: {"tip": 1.0},
                    }
                }
            )
        )
        assert m.load()["recommended"] == {"widgets": {"video": 10.0}}

    def test_a_policy_denial_writes_no_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(m, "_policy_permits", lambda: False)
        m.record_recommended("tip", "artifacts.md")
        assert not m._path().exists()


# -- the real routes ----------------------------------------------------------


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


def _tips_cache():
    from kiro_crew.tips import TipsCache, TipsState
    from kiro_crew.tips_pool import CatalogEntry

    cache = TipsCache()
    cache.catalog = [CatalogEntry(feature="Monitor loops", summary="s", doc="monitor-loops.md")]
    cache.curated = [{"id": "split-view", "doc": "", "doc_link": ""}]
    cache.state = TipsState(
        tips=[{"id": "gen-1", "doc": "slack-integration.md"}],
        offered=_fallback_shape("monitor-loops.md", "Monitor loops"),
    )
    return cache


def _fallback_shape(doc: str, feature: str) -> dict[str, str]:
    """A tip in the shape ``tips._fallback_tips`` builds from one catalog entry."""
    return {
        "id": doc.replace(".md", "-tip"),
        "feature": feature,
        "title": feature,
        "body": "s",
        "why": "",
        "doc": doc,
        "doc_link": "",
        "cta_prompt": f"Tell me about {feature}",
    }


@pytest.mark.asyncio
async def test_tips_feedback_counts_and_feeds_attribution(rec, metrics: list) -> None:
    from kiro_crew.tips import api_tips_feedback

    cache = _tips_cache()
    state = types.SimpleNamespace(_tips_cache=cache)
    resp = await api_tips_feedback(
        _feedback_request(state, {"id": "monitor-loops-tip", "action": "shown"})
    )
    assert resp.status == 200
    # The real feedback write happened, as before.
    assert cache.state.offered is None
    assert cache.state.shown_docs["monitor-loops-tip"] == "monitor-loops.md"
    tips_points = [c for c in rec.counters if c["name"] == fu.TIPS_FEEDBACK]
    assert tips_points == [
        {
            "name": fu.TIPS_FEEDBACK,
            "attrs": {"action": "shown", "source": "catalog", "feature": "monitor-loops.md"},
        }
    ]
    # The route queued the write on an executor; wait for the state it writes.
    await async_wait_until(lambda: "pr-watch" in m.load()["recommended"], off_loop=True)
    # Off the loop, so the hook flush runs inline rather than on an executor.
    await asyncio.to_thread(m.record, "pr-watch")
    assert _reached(metrics, "pr-watch")[0]["recommended_by"] == "tip"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tip_id", "action", "source", "feature"),
    [
        ("split-view", "dismiss", "curated", "other"),
        # In the stored pool but not the offered tip: its doc is read, its source is not.
        ("gen-1", "snooze", "other", "slack-integration.md"),
        ("", "optout", "other", "other"),
        ("unknown-id", "helpful", "other", "other"),
    ],
)
async def test_tips_feedback_labels_each_source(rec, tip_id, action, source, feature) -> None:
    from kiro_crew.tips import api_tips_feedback

    state = types.SimpleNamespace(_tips_cache=_tips_cache())
    resp = await api_tips_feedback(_feedback_request(state, {"id": tip_id, "action": action}))
    assert resp.status == 200
    [point] = [c for c in rec.counters if c["name"] == fu.TIPS_FEEDBACK]
    assert point["attrs"] == {"action": action, "source": source, "feature": feature}


@pytest.mark.asyncio
async def test_a_tips_metric_error_does_not_change_the_response(tmp_path: Path) -> None:
    from kiro_crew.tips import api_tips_feedback

    cache = _tips_cache()
    state = types.SimpleNamespace(_tips_cache=cache)
    with patch(
        "kiro_crew.metrics.feature_usage.record_tip_feedback", side_effect=RuntimeError("x")
    ):
        resp = await api_tips_feedback(
            _feedback_request(state, {"id": "monitor-loops-tip", "action": "ack"})
        )
    assert resp.status == 200
    assert "monitor-loops-tip" in " ".join(cache.state.dismissed)


@pytest.mark.asyncio
async def test_a_dismissed_tip_is_not_a_recommendation(
    rec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.tips import api_tips_feedback

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(m, "record_recommended", lambda c, k: calls.append((c, k)))
    state = types.SimpleNamespace(_tips_cache=_tips_cache())
    for action in ("dismiss", "snooze", "helpful"):
        await api_tips_feedback(_feedback_request(state, {"id": "gen-1", "action": action}))
    assert calls == []
    # The same route does record a "shown" -- so the empty list above is the
    # action filter, not a dead call.
    await api_tips_feedback(_feedback_request(state, {"id": "gen-1", "action": "shown"}))
    assert calls == [("tip", "slack-integration.md")]


def test_video_status_counts_and_feeds_attribution(rec, metrics: list) -> None:
    from kiro_crew import feature_videos as fv

    fv.record_status("monitor-loops", "seen")
    # The status write itself, as before.
    assert fv.load_state().status_of("monitor-loops") == "seen"
    [point] = [c for c in rec.counters if c["name"] == fu.FEATURE_VIDEO_STATUS]
    assert point["attrs"] == {"status": "seen", "feature": "monitor-loops"}
    assert "pr-watch" in m.load()["recommended"]
    m.record("pr-watch")
    assert _reached(metrics, "pr-watch")[0]["recommended_by"] == "video"


def test_a_dismissed_video_counts_but_recommends_nothing(rec) -> None:
    from kiro_crew import feature_videos as fv

    fv.record_status("monitor-loops", "dismissed")
    [point] = [c for c in rec.counters if c["name"] == fu.FEATURE_VIDEO_STATUS]
    assert point["attrs"] == {"status": "dismissed", "feature": "monitor-loops"}
    assert not m._path().exists() or m.load()["recommended"] == {}


def test_an_unknown_video_id_is_never_an_attribute(rec) -> None:
    from kiro_crew import feature_videos as fv

    fv.record_status("a-video-the-catalog-dropped", "seen")
    [point] = [c for c in rec.counters if c["name"] == fu.FEATURE_VIDEO_STATUS]
    assert point["attrs"] == {"status": "seen", "feature": "other"}


def test_a_video_metric_error_does_not_fail_the_write(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import feature_videos as fv

    monkeypatch.setattr(fv, "_feature_of", Mock(side_effect=RuntimeError("x")))
    fv.record_status("monitor-loops", "seen")
    assert fv.load_state().status_of("monitor-loops") == "seen"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("in_pool", "source"),
    # The offered tip is "generated" when the stored pool holds it, and
    # "catalog" when it does not: the route then served the catalog fallback.
    [(True, "generated"), (False, "catalog")],
)
async def test_the_offered_tip_source_is_read_from_the_pool(rec, in_pool, source) -> None:
    from kiro_crew.tips import api_tips_feedback

    cache = _tips_cache()
    offered = {"id": "gen-2", "doc": "subagents.md", "title": "Fan out"}
    cache.state.tips = [dict(offered)] if in_pool else []
    cache.state.offered = offered
    state = types.SimpleNamespace(_tips_cache=cache)
    await api_tips_feedback(_feedback_request(state, {"id": "gen-2", "action": "shown"}))
    [point] = [c for c in rec.counters if c["name"] == fu.TIPS_FEEDBACK]
    assert point["attrs"] == {"action": "shown", "source": source, "feature": "subagents.md"}


class TestConsent:
    @pytest.fixture(autouse=True)
    def _real_consent(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        _floor_monkeypatch.setattr(m, "_recommendation_consent", _REAL_CONSENT)

    def _recorder(self, enabled: bool):
        return patch(
            "kiro_crew.metrics.provider.get_recorder",
            return_value=types.SimpleNamespace(enabled=enabled),
        )

    def test_collection_off_writes_no_recommendation(self, metrics: list) -> None:
        with self._recorder(False):
            m.record_recommended("tip", "artifacts.md")
            m.apply_now(m.op_recommended("video", "monitor-loops"))
            m.apply_now(m.op_starter_clicked("slack", "panel"))
            st = m.load()
            assert st["recommended"] == {}
            # Ordinary milestone progress is unaffected.
            assert st["starter_clicked"]["slack"]
            m.record("widgets")
            assert m.load()["reached"]["widgets"]["source"] == "event"
        assert _reached(metrics, "widgets")[0]["recommended_by"] == "none"

    def test_collection_on_writes_the_stamp(self) -> None:
        with self._recorder(True):
            m.record_recommended("tip", "artifacts.md")
        assert set(m.load()["recommended"]) == {"widgets"}

    def test_an_unreadable_consent_fails_closed(self) -> None:
        with patch("kiro_crew.metrics.provider.get_recorder", side_effect=RuntimeError("x")):
            m.record_recommended("tip", "artifacts.md")
            m.apply_now(m.op_recommended("tip", "artifacts.md"))
        assert not m._path().exists() or m.load()["recommended"] == {}


@pytest.mark.asyncio
async def test_the_tip_that_was_served_is_labelled_not_its_regenerated_namesake(rec) -> None:
    """A refresh can replace the pool under the same id before "shown" arrives."""
    from kiro_crew.tips import api_tips_feedback

    cache = _tips_cache()
    cache.state.offered = {"id": "automation-tip", "doc": "monitor-loops.md", "title": "Watch"}
    cache.state.tips = [{"id": "automation-tip", "doc": "cron-and-scheduling.md", "title": "Cron"}]
    state = types.SimpleNamespace(_tips_cache=cache)
    await api_tips_feedback(_feedback_request(state, {"id": "automation-tip", "action": "shown"}))
    [point] = [c for c in rec.counters if c["name"] == fu.TIPS_FEEDBACK]
    assert point["attrs"]["feature"] == "monitor-loops.md"
    await async_wait_until(lambda: m._path().exists(), off_loop=True)
    await async_wait_until(lambda: "pr-watch" in m.load()["recommended"], off_loop=True)
    assert "scheduled-task" not in m.load()["recommended"]
    # The ack that follows reads the doc "shown" recorded, not the new pool
    # entry. "shown" cleared the offered slot, so its source is not read.
    await api_tips_feedback(_feedback_request(state, {"id": "automation-tip", "action": "ack"}))
    ack = [c for c in rec.counters if c["attrs"].get("action") == "ack"]
    assert ack[0]["attrs"] == {
        "action": "ack",
        "source": "other",
        "feature": "monitor-loops.md",
    }
