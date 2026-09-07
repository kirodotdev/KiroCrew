"""Unit tests for kiro_crew.feature_videos — the deterministic feature-intro catalog."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from body_stream_helpers import attach_body

from kiro_crew import feature_videos as fv


def _request(method: str, path: str, *, session_key: str = "dashboard:ui") -> MagicMock:
    """A mocked dashboard request whose state answers "not restricted"."""
    request = MagicMock()
    state = MagicMock()
    state._restricted_keys = set()
    state._slots = {}
    request.app = {"state": state}
    request.headers = {"X-Session-Key": session_key}
    request.method = method
    request.path = path
    return request


def _cfg(enabled: bool = True) -> MagicMock:
    cfg = MagicMock()
    cfg.dashboard.feature_videos_enabled = enabled
    return cfg


def _entry(video_id: str, **kwargs: object) -> fv.VideoEntry:
    defaults: dict[str, object] = {
        "feature": video_id,
        "title": "t",
        "description": "d",
        "src": f"{fv.ASSET_PREFIX}{video_id}.mp4",
        "poster": f"{fv.ASSET_PREFIX}{video_id}.jpg",
        "duration_s": 5.0,
        "doc": "feature-tips.md",
    }
    defaults.update(kwargs)
    return fv.VideoEntry(id=video_id, **defaults)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _assets_shipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat every catalog asset as present on disk.

    ``offerable()`` withholds an entry whose media is not shipped, and the test
    tree has no ``static/dist``. Without this default every selection and route
    test would exercise the withholding branch instead of what it is about. The
    gate itself is tested in :class:`TestAssetExistenceGate`, which replaces this
    default with a real temp directory.
    """
    monkeypatch.setattr(fv, "_asset_exists", lambda _p: True)


class TestAssetPathValidation:
    """A clip src is fetched by the browser with the dashboard's credentials."""

    @pytest.mark.parametrize(
        "bad",
        [
            "https://cdn.example.com/feature-videos/x.mp4",
            "http://localhost/app-assets/feature-videos/x.mp4",
            "data:video/mp4;base64,AAAA",
            "javascript:alert(1)",
            "//evil.example.com/app-assets/feature-videos/x.mp4",
            "/app-assets/feature-videos//x.mp4",
            "/app-assets/feature-videos/../../secrets.json",
            "/app-assets/feature-videos/..%2fsecrets.json",
            "/app-assets/feature-videos/%2e%2e/secrets.json",
            "\\app-assets\\feature-videos\\x.mp4",
            "/app-assets/feature-videos/x .mp4",
            "/app-assets/feature-videos/x\n.mp4",
            "/static/dist/x.mp4",
            "/app-assets/other/x.mp4",
            "/app-assets/feature-videos/",
            "",
            None,
            42,
        ],
    )
    def test_rejects_unsafe_paths(self, bad: object) -> None:
        assert fv.validate_asset_path(bad) == ""

    @pytest.mark.parametrize(
        "good",
        [
            "/app-assets/feature-videos/feature-tips.mp4",
            "/app-assets/feature-videos/nested/clip.mp4",
            "/app-assets/feature-videos/monitor-loops.jpg",
        ],
    )
    def test_accepts_same_origin_relative_paths(self, good: str) -> None:
        assert fv.validate_asset_path(good) == good


class TestShippedCatalog:
    def test_every_entry_is_valid(self) -> None:
        assert len(fv.catalog()) == len(fv.CATALOG)

    def test_ids_are_unique(self) -> None:
        ids = [e.id for e in fv.CATALOG]
        assert len(ids) == len(set(ids))

    def test_seeded_with_the_two_expected_entries(self) -> None:
        assert [e.id for e in fv.CATALOG] == ["feature-tips", "monitor-loops"]

    def test_docs_are_in_the_tips_allowlist(self) -> None:
        from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

        for entry in fv.CATALOG:
            assert entry.doc in TIP_DOC_ALLOWLIST

    def test_assets_point_at_the_entry_id(self) -> None:
        for entry in fv.CATALOG:
            assert entry.src == f"{fv.ASSET_PREFIX}{entry.id}.mp4"
            assert entry.poster == f"{fv.ASSET_PREFIX}{entry.id}.jpg"

    def test_invalid_entry_is_filtered_not_raised(self) -> None:
        bad = fv.VideoEntry(
            id="bad",
            feature="bad",
            title="t",
            description="d",
            src="https://evil.example.com/x.mp4",
            poster=f"{fv.ASSET_PREFIX}bad.jpg",
            duration_s=1.0,
            doc="feature-tips.md",
        )
        with patch.object(fv, "CATALOG", (bad,) + fv.CATALOG):
            ids = [e.id for e in fv.catalog()]
        assert "bad" not in ids
        assert "feature-tips" in ids

    def test_entry_with_doc_outside_allowlist_is_filtered(self) -> None:
        bad = fv.VideoEntry(
            id="internal",
            feature="internal",
            title="t",
            description="d",
            src=f"{fv.ASSET_PREFIX}internal.mp4",
            poster=f"{fv.ASSET_PREFIX}internal.jpg",
            duration_s=1.0,
            doc="troubleshooting.md",
        )
        with patch.object(fv, "CATALOG", (bad,)):
            assert fv.catalog() == ()

    def test_payload_withholds_used_when(self) -> None:
        payload = fv.CATALOG[0].payload()
        assert "used_when" not in payload
        assert payload["id"] == "feature-tips"


class TestAssetExistenceGate:
    """ "Asset shipped" is a precondition of "on offer"."""

    @pytest.fixture
    def asset_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        # Undo the module-wide default so the real check runs against tmp_path.
        monkeypatch.undo()
        root = tmp_path / "app-assets"
        (root / "feature-videos").mkdir(parents=True)
        monkeypatch.setattr(fv, "_asset_root", lambda: root)
        return root

    @staticmethod
    def _ship(root: Path, video_id: str, *, clip: bool = True, poster: bool = True) -> None:
        d = root / "feature-videos"
        if clip:
            (d / f"{video_id}.mp4").write_bytes(b"\x00")
        if poster:
            (d / f"{video_id}.jpg").write_bytes(b"\x00")

    def test_maps_the_url_prefix_onto_the_dist_directory(self, asset_root: Path) -> None:
        self._ship(asset_root, "x")
        assert fv._asset_exists(f"{fv.ASSET_PREFIX}x.mp4") is True
        assert fv._asset_exists(f"{fv.ASSET_PREFIX}missing.mp4") is False
        # A path outside the served prefix is never "present", whatever is on disk.
        assert fv._asset_exists("/static/x.mp4") is False

    def test_entry_with_no_shipped_media_is_withheld_not_offered(
        self, asset_root: Path, tmp_path: Path
    ) -> None:
        # The frontend cannot catch this: its <video> is preload="none", so no
        # fetch -- and no error -- happens before the user presses play, and the
        # dialog opens on the JSON alone. An unshipped entry must not reach it.
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with patch.object(fv, "CATALOG", (_entry("unshipped"),)):
                assert fv.offerable() == ()
                assert fv.select_next("9.9.9") is None

    def test_a_missing_poster_alone_withholds_the_entry(self, asset_root: Path) -> None:
        self._ship(asset_root, "half", poster=False)
        with patch.object(fv, "CATALOG", (_entry("half"),)):
            assert fv.offerable() == ()

    def test_shipped_entry_is_offered(self, asset_root: Path, tmp_path: Path) -> None:
        self._ship(asset_root, "ready")
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with patch.object(fv, "CATALOG", (_entry("ready"),)):
                picked = fv.select_next("9.9.9")
        assert picked is not None and picked.id == "ready"

    def test_selection_skips_past_an_unshipped_entry_to_a_shipped_one(
        self, asset_root: Path, tmp_path: Path
    ) -> None:
        self._ship(asset_root, "second")
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with patch.object(fv, "CATALOG", (_entry("first"), _entry("second"))):
                picked = fv.select_next("9.9.9")
        assert picked is not None and picked.id == "second"

    def test_withholding_is_recoverable_once_the_clip_lands(
        self, asset_root: Path, tmp_path: Path
    ) -> None:
        # The whole point: nothing is written, so the entry comes back by itself.
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with patch.object(fv, "CATALOG", (_entry("late"),)):
                assert fv.select_next("9.9.9") is None
                self._ship(asset_root, "late")
                picked = fv.select_next("9.9.9")
        assert picked is not None and picked.id == "late"

    def test_catalog_membership_ignores_shipping_so_a_verdict_can_still_land(
        self, asset_root: Path
    ) -> None:
        # A user shown a clip whose asset later vanished must still be able to
        # record a verdict on it: the feedback route checks catalog(), not
        # offerable(), and this pins that the two sets genuinely differ.
        with patch.object(fv, "CATALOG", (_entry("gone"),)):
            assert [e.id for e in fv.catalog()] == ["gone"]
            assert fv.offerable() == ()


class TestProbes:
    def test_unknown_signal_does_not_fire(self) -> None:
        assert fv.probe_fires("no_such_probe") is False

    def test_unknown_parametrized_signal_does_not_fire(self) -> None:
        assert fv.probe_fires("no_such_probe:arg") is False

    def test_raising_probe_counts_as_unused(self) -> None:
        def boom() -> bool:
            raise RuntimeError("probe exploded")

        with patch.dict(fv._PROBES, {"boom": boom}):
            assert fv.probe_fires("boom") is False

    def test_tips_feedback_probe_reads_tips_state(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            state_file = fv.config_dir() / "tips_state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            assert fv.probe_fires("tips_feedback_exists") is False

            state_file.write_text(json.dumps({"shown": {}, "dismissed": []}), encoding="utf-8")
            assert fv.probe_fires("tips_feedback_exists") is False

            state_file.write_text(json.dumps({"dismissed": ["a-tip"]}), encoding="utf-8")
            assert fv.probe_fires("tips_feedback_exists") is True

    def test_tips_feedback_probe_fires_on_opt_out(self, tmp_path: Path) -> None:
        """Opting out is the strongest reaction, and the only one writing no collection."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            state_file = fv.config_dir() / "tips_state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            # Exactly what tips writes on optout from a fresh install: every
            # collection empty and the cadence stamp still 0.0.
            state_file.write_text(
                json.dumps(
                    {
                        "opted_out": True,
                        "shown": {},
                        "dismissed": [],
                        "dismissed_docs": [],
                        "snoozed": {},
                        "snoozed_docs": {},
                        "last_shown_ts": 0.0,
                    }
                ),
                encoding="utf-8",
            )
            assert fv.probe_fires("tips_feedback_exists") is True

    def test_opt_out_withdraws_the_tips_video(self, tmp_path: Path) -> None:
        # The catalog is pinned to the tips entry plus one signal-free control.
        # Running against the SHIPPED catalog would put the second entry's
        # sel_event_seen probe on the host's real audit log, so a box that has
        # ever called monitor_start withdraws both entries and the assertion
        # reads as a failure of the opt-out path it is not testing.
        control = _entry("control")
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            state_file = fv.config_dir() / "tips_state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({"opted_out": True}), encoding="utf-8")
            with patch.object(fv, "CATALOG", (fv.CATALOG[0], control)):
                picked = fv.select_next("9.9.9")
        assert picked is not None and picked.id == "control"

    def test_opted_out_false_does_not_fire(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            state_file = fv.config_dir() / "tips_state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({"opted_out": False}), encoding="utf-8")
            assert fv.probe_fires("tips_feedback_exists") is False

    def test_tips_feedback_probe_fires_on_last_shown_ts(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            state_file = fv.config_dir() / "tips_state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({"last_shown_ts": 1.0}), encoding="utf-8")
            assert fv.probe_fires("tips_feedback_exists") is True

    def test_tips_feedback_probe_tolerates_a_corrupt_file(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            state_file = fv.config_dir() / "tips_state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text("{not json", encoding="utf-8")
            assert fv.probe_fires("tips_feedback_exists") is False

    def test_artifacts_probe_fires_only_on_a_real_artifact(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            root = fv.config_dir() / "artifacts"
            root.mkdir(parents=True, exist_ok=True)
            assert fv.probe_fires("artifacts_nonempty") is False

            # A dot-directory (the store's own scratch) is not an artifact.
            (root / ".trash").mkdir()
            assert fv.probe_fires("artifacts_nonempty") is False

            slug = root / "cr-queue"
            slug.mkdir()
            assert fv.probe_fires("artifacts_nonempty") is False  # no meta.json yet
            (slug / "meta.json").write_text("{}", encoding="utf-8")
            assert fv.probe_fires("artifacts_nonempty") is True

    def test_config_key_probe_reads_the_file_not_the_default(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cfg_file = fv.config_path()
            cfg_file.parent.mkdir(parents=True, exist_ok=True)
            cfg_file.write_text(json.dumps({"dashboard": {}}), encoding="utf-8")
            # tips_enabled has a shipped default of True but is absent here, so
            # the probe must NOT fire — otherwise every install looks configured.
            assert fv.probe_fires("config_key_set:dashboard.tips_enabled") is False

            cfg_file.write_text(
                json.dumps({"dashboard": {"tips_enabled": False}}), encoding="utf-8"
            )
            assert fv.probe_fires("config_key_set:dashboard.tips_enabled") is True

    def test_config_key_probe_also_reads_the_local_overlay(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            overlay = fv.config_local_path()
            overlay.parent.mkdir(parents=True, exist_ok=True)
            overlay.write_text(json.dumps({"agent": {"acp_backend": "kiro"}}), encoding="utf-8")
            assert fv.probe_fires("config_key_set:agent.acp_backend") is True

    def test_config_key_probe_with_empty_path_does_not_fire(self) -> None:
        assert fv.probe_fires("config_key_set:") is False

    def test_sel_probe_matches_the_tool_operation(self) -> None:
        fake_log = MagicMock()
        fake_log.recent.return_value = [
            {"event_type": "tool_invocation", "operation": "execute_bash"},
            "not-a-dict",
            {"event_type": "tool_invocation", "operation": "monitor_start"},
        ]
        with patch.object(fv, "sel", return_value=fake_log):
            assert fv.probe_fires("sel_event_seen:monitor_start") is True
            assert fv.probe_fires("sel_event_seen:cron_add") is False

    def test_sel_probe_read_is_bounded(self) -> None:
        fake_log = MagicMock()
        fake_log.recent.return_value = []
        with patch.object(fv, "sel", return_value=fake_log):
            fv.probe_fires("sel_event_seen:monitor_start")
        fake_log.recent.assert_called_once_with(limit=fv._SEL_PROBE_LIMIT)

    def test_sel_probe_with_empty_tool_name_does_not_fire(self) -> None:
        assert fv.probe_fires("sel_event_seen:") is False


class TestState:
    def test_missing_file_loads_empty(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            assert fv.load_state().videos == {}

    def test_round_trip(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            st = fv.FeatureVideoState(videos={"feature-tips": {"status": "seen", "ts": 12.5}})
            fv.save_state(st)
            assert fv.load_state().status_of("feature-tips") == "seen"

    def test_state_file_is_owner_only(self, tmp_path: Path) -> None:
        if sys.platform.startswith("win"):
            pytest.skip("POSIX mode bits are a no-op on Windows")
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            fv.save_state(fv.FeatureVideoState(videos={"feature-tips": {"status": "seen"}}))
            mode = stat.S_IMODE(fv._state_path().stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {mode:o}"

    @pytest.mark.parametrize(
        "raw",
        [
            "[]",
            '"a string"',
            "{not json",
            '{"videos": 3}',
            '{"videos": {"x": 3}}',
            '{"videos": {"x": {"status": "snoozed"}}}',
            '{"videos": {"x": {"ts": 1}}}',
        ],
    )
    def test_malformed_state_degrades_to_empty(self, tmp_path: Path, raw: str) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            path = fv._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(raw, encoding="utf-8")
            assert fv.load_state().videos == {}

    def test_non_numeric_ts_is_zeroed_not_dropped(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            path = fv._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"videos": {"feature-tips": {"status": "seen", "ts": "soon"}}}),
                encoding="utf-8",
            )
            st = fv.load_state()
        assert st.status_of("feature-tips") == "seen"
        assert st.videos["feature-tips"]["ts"] == 0.0

    def test_an_unfloatable_ts_does_not_crash_the_loader(self, tmp_path: Path) -> None:
        """A several-hundred-digit int raises OverflowError, not ValueError."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            path = fv._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '{"videos": {"feature-tips": {"status": "seen", "ts": ' + "9" * 400 + "}}}",
                encoding="utf-8",
            )
            st = fv.load_state()
        assert st.status_of("feature-tips") == "seen"
        assert st.videos["feature-tips"]["ts"] == 0.0

    def test_concurrent_records_keep_both_rows(self, tmp_path: Path) -> None:
        """Load-mutate-save is one transaction, so neither writer drops the other."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            fv._state_path().parent.mkdir(parents=True, exist_ok=True)
            barrier = threading.Barrier(2)

            def record(video_id: str, status: str) -> None:
                barrier.wait(timeout=10)
                fv.record_status(video_id, status)

            threads = [
                threading.Thread(target=record, args=("feature-tips", "dismissed")),
                threading.Thread(target=record, args=("monitor-loops", "seen")),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            st = fv.load_state()
        assert st.status_of("feature-tips") == "dismissed"
        assert st.status_of("monitor-loops") == "seen"

    def test_deeply_nested_state_does_not_crash_the_loader(self, tmp_path: Path) -> None:
        """json.loads raises RecursionError, which is neither OSError nor ValueError."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            path = fv._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            depth = sys.getrecursionlimit() * 3
            path.write_text("[" * depth + "]" * depth, encoding="utf-8")
            assert fv.load_state().videos == {}

    def test_deeply_nested_tips_state_does_not_crash_the_probe(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            tips_state = fv.config_dir() / "tips_state.json"
            tips_state.parent.mkdir(parents=True, exist_ok=True)
            depth = sys.getrecursionlimit() * 3
            tips_state.write_text("[" * depth + "]" * depth, encoding="utf-8")
            assert fv.probe_fires("tips_feedback_exists") is False

    def test_deeply_nested_config_does_not_crash_the_probe(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            cfg_file = fv.config_path()
            cfg_file.parent.mkdir(parents=True, exist_ok=True)
            depth = sys.getrecursionlimit() * 3
            cfg_file.write_text("[" * depth + "]" * depth, encoding="utf-8")
            assert fv.probe_fires("config_key_set:dashboard.tips_enabled") is False

    def test_state_lives_beside_tips_state(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            assert fv._state_path().name == "feature_videos_state.json"
            assert fv._state_path().parent == fv.config_dir()


class TestVersionGate:
    def test_no_floor_always_passes(self) -> None:
        assert fv._version_ok("", "0.1.0") is True

    def test_running_below_floor_fails(self) -> None:
        assert fv._version_ok("2.0.0", "1.9.9") is False

    def test_running_at_or_above_floor_passes(self) -> None:
        assert fv._version_ok("2.0.0", "2.0.0") is True
        assert fv._version_ok("2.0.0", "2.1.0") is True

    def test_prerelease_suffix_on_running_version_is_stripped(self) -> None:
        assert fv._version_ok("2.0.0", "2.0.0rc3") is True

    def test_unparseable_floor_fails_closed(self) -> None:
        assert fv._version_ok("not-a-version", "9.9.9") is False

    def test_unparseable_running_version_passes(self) -> None:
        assert fv._version_ok("1.0.0", "unknown") is True


class TestSelectNext:
    def test_returns_the_first_eligible_entry_in_catalog_order(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with patch.object(fv, "CATALOG", (_entry("first"), _entry("second"))):
                picked = fv.select_next("9.9.9")
        assert picked is not None and picked.id == "first"

    def test_skips_seen_and_dismissed(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            fv.save_state(
                fv.FeatureVideoState(
                    videos={
                        "first": {"status": "seen", "ts": 1.0},
                        "second": {"status": "dismissed", "ts": 2.0},
                    }
                )
            )
            with patch.object(fv, "CATALOG", (_entry("first"), _entry("second"), _entry("third"))):
                picked = fv.select_next("9.9.9")
        assert picked is not None and picked.id == "third"

    def test_skips_a_used_feature(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with (
                patch.object(
                    fv,
                    "CATALOG",
                    (_entry("used", used_when=("always",)), _entry("unused")),
                ),
                patch.dict(fv._PROBES, {"always": lambda: True}),
            ):
                picked = fv.select_next("9.9.9")
        assert picked is not None and picked.id == "unused"

    def test_any_firing_signal_withdraws_the_entry(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with (
                patch.object(
                    fv,
                    "CATALOG",
                    (_entry("used", used_when=("never", "always")), _entry("unused")),
                ),
                patch.dict(fv._PROBES, {"always": lambda: True, "never": lambda: False}),
            ):
                picked = fv.select_next("9.9.9")
        assert picked is not None and picked.id == "unused"

    def test_probes_are_not_run_for_an_already_seen_entry(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def probe() -> bool:
            calls.append("ran")
            return False

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            fv.save_state(fv.FeatureVideoState(videos={"first": {"status": "seen", "ts": 1.0}}))
            with (
                patch.object(fv, "CATALOG", (_entry("first", used_when=("p",)),)),
                patch.dict(fv._PROBES, {"p": probe}),
            ):
                assert fv.select_next("9.9.9") is None
        assert calls == []

    def test_skips_a_version_gated_entry(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with patch.object(
                fv, "CATALOG", (_entry("future", min_version="99.0.0"), _entry("now"))
            ):
                picked = fv.select_next("1.2.3")
        assert picked is not None and picked.id == "now"

    def test_returns_none_when_everything_is_exhausted(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            fv.save_state(fv.FeatureVideoState(videos={"only": {"status": "seen", "ts": 1.0}}))
            with patch.object(fv, "CATALOG", (_entry("only"),)):
                assert fv.select_next("9.9.9") is None

    def test_selection_is_stable_across_calls(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with patch.object(fv, "CATALOG", (_entry("a"), _entry("b"), _entry("c"))):
                picks = {(fv.select_next("9.9.9") or _entry("x")).id for _ in range(5)}
        assert picks == {"a"}


class TestNextRoute:
    def test_kill_switch_reports_disabled_and_no_video(self, tmp_path: Path) -> None:
        async def run() -> dict[str, object]:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                with patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls:
                    cfg_cls.load.return_value = _cfg(enabled=False)
                    resp = await fv.api_feature_videos_next(
                        _request("GET", "/api/feature-videos/next")
                    )
            assert resp.status == 200
            return json.loads(resp.body)  # type: ignore[arg-type]

        body = asyncio.run(run())
        assert body == {"video": None, "enabled": False}

    def test_serves_the_first_eligible_entry(self, tmp_path: Path) -> None:
        async def run() -> dict[str, object]:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                with (
                    patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls,
                    patch.object(fv, "CATALOG", (_entry("first"),)),
                ):
                    cfg_cls.load.return_value = _cfg()
                    resp = await fv.api_feature_videos_next(
                        _request("GET", "/api/feature-videos/next")
                    )
            return json.loads(resp.body)  # type: ignore[arg-type]

        body = asyncio.run(run())
        assert body["enabled"] is True
        assert isinstance(body["video"], dict)
        assert body["video"]["id"] == "first"
        assert "used_when" not in body["video"]

    def test_restricted_session_gets_null_but_stays_enabled(self, tmp_path: Path) -> None:
        async def run() -> dict[str, object]:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                with (
                    patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls,
                    patch.object(fv, "_is_restricted_session", return_value=True),
                    patch.object(fv, "CATALOG", (_entry("first"),)),
                ):
                    cfg_cls.load.return_value = _cfg()
                    resp = await fv.api_feature_videos_next(
                        _request("GET", "/api/feature-videos/next", session_key="chat-1")
                    )
            return json.loads(resp.body)  # type: ignore[arg-type]

        assert asyncio.run(run()) == {"video": None, "enabled": True}

    def test_null_when_catalog_is_exhausted(self, tmp_path: Path) -> None:
        async def run() -> dict[str, object]:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                fv.save_state(
                    fv.FeatureVideoState(videos={"only": {"status": "dismissed", "ts": 1.0}})
                )
                with (
                    patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls,
                    patch.object(fv, "CATALOG", (_entry("only"),)),
                ):
                    cfg_cls.load.return_value = _cfg()
                    resp = await fv.api_feature_videos_next(
                        _request("GET", "/api/feature-videos/next")
                    )
            return json.loads(resp.body)  # type: ignore[arg-type]

        assert asyncio.run(run()) == {"video": None, "enabled": True}


class TestStatusRoute:
    def test_reports_enabled_and_state(self, tmp_path: Path) -> None:
        async def run() -> dict[str, object]:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                fv.save_state(
                    fv.FeatureVideoState(videos={"feature-tips": {"status": "seen", "ts": 7.0}})
                )
                with patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls:
                    cfg_cls.load.return_value = _cfg()
                    resp = await fv.api_feature_videos_status(
                        _request("GET", "/api/feature-videos/status")
                    )
            assert resp.status == 200
            return json.loads(resp.body)  # type: ignore[arg-type]

        body = asyncio.run(run())
        assert body["enabled"] is True
        assert body["state"] == {"feature-tips": {"status": "seen", "ts": 7.0}}

    def test_a_read_blocking_session_gets_no_history(self, tmp_path: Path) -> None:
        """The state map is engagement history, so a temporary session is served none."""

        async def run() -> dict[str, object]:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                fv.save_state(
                    fv.FeatureVideoState(videos={"feature-tips": {"status": "seen", "ts": 7.0}})
                )
                with (
                    patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls,
                    patch.object(fv, "_blocks_reads_session", return_value=True),
                ):
                    cfg_cls.load.return_value = _cfg()
                    resp = await fv.api_feature_videos_status(
                        _request("GET", "/api/feature-videos/status", session_key="chat-1")
                    )
                assert resp.status == 200
                return json.loads(resp.body)  # type: ignore[arg-type]

        body = asyncio.run(run())
        # enabled stays truthful -- the kill switch is config, not history.
        assert body == {"enabled": True, "state": {}}

    def test_an_incognito_session_still_reads_its_own_panel(self, tmp_path: Path) -> None:
        """Incognito withholds WRITES; only a read-blocking session loses the map."""

        async def run() -> dict[str, object]:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                fv.save_state(
                    fv.FeatureVideoState(videos={"feature-tips": {"status": "seen", "ts": 7.0}})
                )
                with (
                    patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls,
                    patch.object(fv, "_is_restricted_session", return_value=True),
                    patch.object(fv, "_blocks_reads_session", return_value=False),
                ):
                    cfg_cls.load.return_value = _cfg()
                    resp = await fv.api_feature_videos_status(
                        _request("GET", "/api/feature-videos/status", session_key="chat-2")
                    )
                return json.loads(resp.body)  # type: ignore[arg-type]

        body = asyncio.run(run())
        assert body["state"] == {"feature-tips": {"status": "seen", "ts": 7.0}}

    def test_reports_the_kill_switch(self, tmp_path: Path) -> None:
        async def run() -> dict[str, object]:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                with patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls:
                    cfg_cls.load.return_value = _cfg(enabled=False)
                    resp = await fv.api_feature_videos_status(
                        _request("GET", "/api/feature-videos/status")
                    )
            return json.loads(resp.body)  # type: ignore[arg-type]

        body = asyncio.run(run())
        assert body["enabled"] is False
        assert body["state"] == {}


def _feedback_request(body: object) -> MagicMock:
    request = _request("POST", "/api/feature-videos/feedback")
    attach_body(request, body)
    return request


class TestFeedbackRoute:
    @pytest.mark.parametrize("status", ["seen", "dismissed"])
    def test_records_both_statuses_permanently(self, tmp_path: Path, status: str) -> None:
        async def run() -> tuple[dict[str, object], str, object, float]:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                before = time.time()
                resp = await fv.api_feature_videos_feedback(
                    _feedback_request({"id": "feature-tips", "status": status})
                )
                assert resp.status == 200
                st = fv.load_state()
            body = json.loads(resp.body)  # type: ignore[arg-type]
            return body, st.status_of("feature-tips"), st.videos["feature-tips"]["ts"], before

        body, recorded, ts, before = asyncio.run(run())
        assert body == {"ok": True}
        assert recorded == status
        assert isinstance(ts, float) and ts >= before

    def test_recorded_video_is_not_offered_again(self, tmp_path: Path) -> None:
        async def run() -> object:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                with (
                    patch("kiro_crew.feature_videos.KiroCrewConfig") as cfg_cls,
                    patch.object(fv, "CATALOG", (_entry("first"), _entry("second"))),
                ):
                    cfg_cls.load.return_value = _cfg()
                    first = json.loads(
                        (
                            await fv.api_feature_videos_next(
                                _request("GET", "/api/feature-videos/next")
                            )
                        ).body  # type: ignore[arg-type]
                    )
                    assert first["video"]["id"] == "first"
                    await fv.api_feature_videos_feedback(
                        _feedback_request({"id": "first", "status": "dismissed"})
                    )
                    second = json.loads(
                        (
                            await fv.api_feature_videos_next(
                                _request("GET", "/api/feature-videos/next")
                            )
                        ).body  # type: ignore[arg-type]
                    )
            return second["video"]["id"]

        assert asyncio.run(run()) == "second"

    def test_state_file_written_by_feedback_is_owner_only(self, tmp_path: Path) -> None:
        if sys.platform.startswith("win"):
            pytest.skip("POSIX mode bits are a no-op on Windows")

        async def run() -> int:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                await fv.api_feature_videos_feedback(
                    _feedback_request({"id": "monitor-loops", "status": "seen"})
                )
                return stat.S_IMODE(fv._state_path().stat().st_mode)

        assert asyncio.run(run()) == 0o600

    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"id": "no-such-video", "status": "seen"}, "unknown_video"),
            ({"id": "feature-tips", "status": "snoozed"}, "invalid_status"),
            ({"id": "feature-tips", "status": ""}, "invalid_status"),
            ({"id": "feature-tips"}, "invalid_status"),
            ({"status": "seen"}, "unknown_video"),
            ({"id": ["feature-tips"], "status": "seen"}, "invalid_field_type"),
            ({"id": "feature-tips", "status": 3}, "invalid_field_type"),
            # An oversized id is refused by catalog membership, not by a
            # length branch: one code covers every id that is not a slug.
            ({"id": "x" * 101, "status": "seen"}, "unknown_video"),
        ],
    )
    def test_bad_bodies_are_400_with_a_code(
        self, tmp_path: Path, body: dict[str, object], code: str
    ) -> None:
        async def run() -> tuple[int, dict[str, object]]:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                resp = await fv.api_feature_videos_feedback(_feedback_request(body))
            return resp.status, json.loads(resp.body)  # type: ignore[arg-type]

        status, payload = asyncio.run(run())
        assert status == 400
        assert payload["code"] == code

    def test_an_oversized_id_is_refused_as_unknown(self, tmp_path: Path) -> None:
        """One code for every non-slug id: membership is tighter than a length bound."""

        async def run() -> tuple[dict[str, object], bool]:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                resp = await fv.api_feature_videos_feedback(
                    _feedback_request({"id": "y" * 5000, "status": "seen"})
                )
                assert resp.status == 400
                return json.loads(resp.body), fv._state_path().exists()  # type: ignore[arg-type,return-value]

        payload, wrote = asyncio.run(run())
        assert payload["code"] == "unknown_video"
        assert wrote is False

    def test_a_rejected_body_writes_no_state(self, tmp_path: Path) -> None:
        async def run() -> bool:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                await fv.api_feature_videos_feedback(
                    _feedback_request({"id": "no-such-video", "status": "seen"})
                )
                return fv._state_path().exists()

        assert asyncio.run(run()) is False

    def test_a_restricted_session_records_nothing(self, tmp_path: Path) -> None:
        """The write side needs the same gate /next has — it is where the trace lands."""

        async def run() -> tuple[int, dict[str, object], bool]:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                with patch.object(fv, "_is_restricted_session", return_value=True):
                    resp = await fv.api_feature_videos_feedback(
                        _feedback_request({"id": "feature-tips", "status": "seen"})
                    )
                return resp.status, json.loads(resp.body), fv._state_path().exists()  # type: ignore[arg-type,return-value]

        status, payload, wrote = asyncio.run(run())
        assert status == 200
        assert payload == {"ok": True}
        assert wrote is False

    def test_non_object_body_is_rejected(self, tmp_path: Path) -> None:
        async def run() -> int:
            with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
                resp = await fv.api_feature_videos_feedback(_feedback_request(["feature-tips"]))
            return resp.status

        assert asyncio.run(run()) == 400


class TestRouteRegistration:
    def test_the_three_routes_are_registered_next_to_tips(self) -> None:
        source = (
            Path(fv.__file__).resolve().parent / "dashboard" / "routes" / "realtime.py"
        ).read_text(encoding="utf-8")
        assert '"/api/feature-videos/next", api_feature_videos_next' in source
        assert '"/api/feature-videos/status", api_feature_videos_status' in source
        assert '"/api/feature-videos/feedback", api_feature_videos_feedback' in source

    def test_the_shared_config_endpoints_are_untouched(self) -> None:
        source = Path(fv.__file__).read_text(encoding="utf-8")
        assert "social_share" not in source
        assert "/api/dashboard/config" not in source


class TestConfigFlag:
    def test_default_is_off(self) -> None:
        from kiro_crew.config.sections import DashboardConfig

        assert DashboardConfig().feature_videos_enabled is False

    def test_loader_reads_the_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"dashboard": {"feature_videos_enabled": False}}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        assert KiroCrewConfig.load().dashboard.feature_videos_enabled is False

    def test_absent_key_keeps_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {}}), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        assert KiroCrewConfig.load().dashboard.feature_videos_enabled is False

    def test_a_non_bool_value_keeps_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured string must not read as "on" -- bool("false") is True.

        Still load-bearing with the default OFF: a naive ``bool(...)`` would read the
        STRING "false" as True and turn the feature on against the operator's config.
        """
        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"dashboard": {"feature_videos_enabled": "false"}}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        assert KiroCrewConfig.load().dashboard.feature_videos_enabled is False
