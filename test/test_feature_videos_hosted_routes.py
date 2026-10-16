"""Hosted feature videos: the ``/next``, ``/status`` and ``/fetch-all`` dashboard routes,
and the governance probe they read.
``test_feature_videos_hosted.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from aiohttp import web
from feature_videos_hosted_helpers import _fresh_cache  # noqa: F401 - fixture
from feature_videos_hosted_helpers import _no_network  # noqa: F401 - fixture
from feature_videos_hosted_helpers import _entry, _parsed, _seed, _write_media

from kiro_crew import feature_videos as fv
from kiro_crew import feature_videos_cache as cache_mod
from kiro_crew import feature_videos_manifest as manifest_mod

# ── next, status and fetch-all ──


def _dashboard_request(path: str = "/api/feature-videos/status") -> MagicMock:
    request = MagicMock()
    state = MagicMock()
    state._restricted_keys = set()
    state._slots = {}
    request.app = {"state": state}
    request.headers = {"X-Session-Key": "dashboard:ui"}
    request.method = "GET"
    request.path = path
    return request


def _next_body(tmp_path: Path) -> dict[str, object]:
    cfg = MagicMock()
    cfg.dashboard.feature_videos_enabled = True
    with patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls:
        cfg_cls.load.return_value = cfg
        resp = asyncio.run(
            fv.api_feature_videos_next(_dashboard_request("/api/feature-videos/next"))
        )
    return json.loads(resp.body)  # type: ignore[arg-type]


class TestNextRouteReportsNoDownloadPolicy:
    """``/next`` answers what to play; whether bytes may be pulled is ``/status``'s.

    The frontend's ``FeatureVideoNext.download_enabled`` is optional and an
    absent value reads as off, so nothing is owed here — and the governance memo
    is not read on a route that authorizes no egress.
    """

    def test_an_offer_carries_only_the_clip_and_the_switch(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            _seed(_parsed())
            with (
                patch.object(manifest_mod, "download_permitted_cached", side_effect=AssertionError),
                patch.object(manifest_mod, "download_denied", side_effect=AssertionError),
            ):
                body = _next_body(tmp_path)
        assert set(body) == {"video", "enabled"}
        assert body["enabled"] is True
        assert body["video"]["id"] == "hosted-clip"  # type: ignore[index]
        assert "source" not in body["video"]  # type: ignore[operator]
        assert body["video"]["src"] == "/feature-videos/0.6.0/hosted-clip.mp4"  # type: ignore[index]

    def test_a_denied_ceiling_still_offers_nothing_uncached(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            with patch.object(manifest_mod, "download_denied", return_value=True):
                body = _next_body(tmp_path)
        assert body == {"video": None, "enabled": True}

    def test_a_cached_clip_under_a_denial_still_plays_locally(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            _seed(_parsed())
            with patch.object(manifest_mod, "download_denied", return_value=True):
                body = _next_body(tmp_path)
        assert body["video"]["src"] == "/feature-videos/0.6.0/hosted-clip.mp4"  # type: ignore[index]

    def test_without_a_manifest_no_governance_is_consulted(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with (
                patch.object(fv, "_asset_exists", lambda _p: True),
                patch.object(manifest_mod, "download_permitted_cached", side_effect=AssertionError),
                patch.object(manifest_mod, "download_denied", side_effect=AssertionError),
            ):
                body = _next_body(tmp_path)
        assert body["video"] is not None
        assert set(body) == {"video", "enabled"}

    def test_the_kill_switch_answer_is_the_same_two_fields(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = False
            with (
                patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", side_effect=AssertionError),
                patch.object(manifest_mod, "download_permitted_cached", side_effect=AssertionError),
            ):
                cfg_cls.load.return_value = cfg
                resp = asyncio.run(
                    fv.api_feature_videos_next(_dashboard_request("/api/feature-videos/next"))
                )
        assert json.loads(resp.body) == {"video": None, "enabled": False}  # type: ignore[arg-type]


class TestStatusRoute:
    def test_reports_the_release_and_the_cache_progress(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0", "cached-one")
            _seed(_parsed(entries=[_entry("cached-one"), _entry("uncached-one")]))
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = True
            # The memo is process-wide and TTL'd, so a sibling test that just
            # evaluated a denial would otherwise leak into this readout.
            with (
                patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_permitted_cached", return_value=True),
            ):
                cfg_cls.load.return_value = cfg
                resp = asyncio.run(fv.api_feature_videos_status(_dashboard_request()))
            body = json.loads(resp.body)  # type: ignore[arg-type]
        assert body["release"] == "0.6.0"
        assert (body["cached"], body["total"]) == (1, 2)
        assert body["download_enabled"] is True
        assert body["downloading"] is None
        assert body["downloading_title"] is None
        assert body["download_state"] == cache_mod.STATE_IDLE

    def _status_while(self, tmp_path: Path, entry: manifest_mod.ManifestEntry) -> dict:
        """The status body with *entry* recorded as the clip in flight."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = True
            cache = cache_mod.feature_video_cache()
            cache._set_state(
                cache_mod.STATE_DOWNLOADING, downloading=entry.id, downloading_title=entry.title
            )
            try:
                with (
                    patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls,
                    patch.object(manifest_mod, "download_permitted_cached", return_value=True),
                ):
                    cfg_cls.load.return_value = cfg
                    resp = asyncio.run(fv.api_feature_videos_status(_dashboard_request()))
            finally:
                cache._set_state(cache_mod.STATE_IDLE)
            return json.loads(resp.body)  # type: ignore[arg-type]

    def test_reports_the_title_of_the_clip_in_flight(self, tmp_path: Path) -> None:
        """The panel names the clip by its title; the id alone reads like developer output."""
        entry = _parsed(entries=[_entry("monitor-loops", title="Monitor loops")]).entries[0]
        body = self._status_while(tmp_path, entry)
        assert body["downloading"] == "monitor-loops"
        assert body["downloading_title"] == "Monitor loops"

    def test_an_untitled_clip_reports_no_title_rather_than_an_empty_one(
        self, tmp_path: Path
    ) -> None:
        entry = _parsed(entries=[_entry("untitled", title="")]).entries[0]
        body = self._status_while(tmp_path, entry)
        assert body["downloading"] == "untitled"
        assert body["downloading_title"] is None

    def test_a_state_with_nothing_in_flight_carries_no_title(self) -> None:
        cache = cache_mod.FeatureVideoCache()
        cache._set_state(cache_mod.STATE_READY, downloading_title="stale")
        assert cache.status["downloading"] is None
        assert cache.status["downloading_title"] is None

    def test_reports_a_denied_ceiling(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = True
            with (
                patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_permitted_cached", return_value=False),
            ):
                cfg_cls.load.return_value = cfg
                resp = asyncio.run(fv.api_feature_videos_status(_dashboard_request()))
            body = json.loads(resp.body)  # type: ignore[arg-type]
        assert body["download_enabled"] is False


def _enabled_cfg(enabled: bool = True) -> MagicMock:
    cfg = MagicMock()
    cfg.dashboard.feature_videos_enabled = enabled
    return cfg


class TestFetchAllRoute:
    def test_the_kill_switch_is_a_409_and_starts_nothing(self, tmp_path: Path) -> None:
        """A silent ok would leave the panel polling for bytes that never come."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with (
                patch("kiro_crew.feature_videos_cache.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", side_effect=AssertionError),
                patch.object(cache_mod.FeatureVideoCache, "ensure_all", side_effect=AssertionError),
            ):
                cfg_cls.load.return_value = _enabled_cfg(False)
                resp = asyncio.run(cache_mod.api_feature_videos_fetch_all(MagicMock()))
            body = json.loads(resp.body)  # type: ignore[arg-type]
        assert resp.status == 409
        assert body["code"] == "feature_disabled"
        assert cache_mod.feature_video_cache().status["download_state"] == cache_mod.STATE_DISABLED

    def test_a_denied_ceiling_is_a_403_and_starts_nothing(self, tmp_path: Path) -> None:
        """An ACTION chokepoint, so it takes the audited answer, not the polled memo."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with (
                patch("kiro_crew.feature_videos_cache.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", return_value=True),
                patch.object(cache_mod.FeatureVideoCache, "ensure_all", side_effect=AssertionError),
            ):
                cfg_cls.load.return_value = _enabled_cfg()
                resp = asyncio.run(cache_mod.api_feature_videos_fetch_all(MagicMock()))
            body = json.loads(resp.body)  # type: ignore[arg-type]
        assert resp.status == 403
        assert body["code"] == "governance_denied"
        assert cache_mod.feature_video_cache().status["download_state"] == cache_mod.STATE_DENIED

    def test_a_permitted_call_starts_one_unpaced_pass(self, tmp_path: Path) -> None:
        calls: list[bool] = []

        async def _fake_ensure_all(self: object, *, unlimited: bool = False) -> bool:
            calls.append(unlimited)
            return True

        async def run() -> web.Response:
            with (
                patch("kiro_crew.feature_videos_cache.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", return_value=False),
                patch.object(cache_mod.FeatureVideoCache, "ensure_all", _fake_ensure_all),
            ):
                cfg_cls.load.return_value = _enabled_cfg()
                resp = await cache_mod.api_feature_videos_fetch_all(MagicMock())
                assert cache_mod._task is not None
                await cache_mod._task
                return resp

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            resp = asyncio.run(run())
        assert resp.status == 200
        assert calls == [True]


# ── governance probe itself ──


class TestGovernanceProbe:
    def test_the_scope_is_in_the_catalog_as_a_capability(self) -> None:
        from kiro_crew.platform.governance import SCOPE_CATALOG

        spec = SCOPE_CATALOG[manifest_mod.DOWNLOAD_SCOPE]
        assert spec.capability_default is True

    def test_a_permitting_decision_reads_as_not_denied(self) -> None:
        decision = SimpleNamespace(permitted=True)
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit", return_value=decision
        ) as vet:
            assert manifest_mod.download_denied() is False
        # Fail-closed posture and the pinned surface are part of the contract.
        assert vet.call_args.kwargs["fail_closed"] is True
        assert vet.call_args.kwargs["session_key"] == manifest_mod.DASHBOARD_SURFACE_KEY
        assert vet.call_args.kwargs["tool_name"] == manifest_mod.AUDIT_TOOL

    def test_a_denying_decision_denies(self) -> None:
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=SimpleNamespace(permitted=False),
        ):
            assert manifest_mod.download_denied() is True

    def test_an_unevaluable_ceiling_denies_and_is_audited(self) -> None:
        with (
            patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit",
                side_effect=RuntimeError("composition failed"),
            ),
            patch("kiro_crew.sel.sel") as sel_factory,
        ):
            assert manifest_mod.download_denied() is True
        sel_factory.return_value.log_governance_decision.assert_called_once()
        kwargs = sel_factory.return_value.log_governance_decision.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["scope"] == manifest_mod.DOWNLOAD_SCOPE

    def test_the_cached_read_tracks_the_last_real_answer(self) -> None:
        """A polled read reports what the last decision was, without deciding again."""
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=SimpleNamespace(permitted=True),
        ):
            manifest_mod.download_denied()
        assert manifest_mod.download_permitted_cached() is True
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=SimpleNamespace(permitted=False),
        ):
            manifest_mod.download_denied()
        assert manifest_mod.download_permitted_cached() is False
