"""Hosted feature videos: what counts as cached (both files, the right size, a matching
receipt), the verified poster-then-clip download pass, and the governance ceiling
re-read per clip while a paced pass runs.
``test_feature_videos_hosted.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import asyncio
import os
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from feature_videos_hosted_helpers import _fresh_cache  # noqa: F401 - fixture
from feature_videos_hosted_helpers import _no_network  # noqa: F401 - fixture
from feature_videos_hosted_helpers import (
    _CLIP,
    _CLIP_SHA,
    _POSTER,
    _as_opener,
    _entry,
    _parsed,
    _write_media,
)

from kiro_crew import asset_downloader
from kiro_crew import feature_videos_cache as cache_mod
from kiro_crew import feature_videos_manifest as manifest_mod

# ── cached-ness and the download pass ──


def _fake_media_fetch(available: "set[str]"):
    class _Resp:
        def __init__(self, body: bytes) -> None:
            self._body = body
            self._pos = 0
            self.status = 200
            self.headers = {"Content-Length": str(len(body))}

        def read(self, n: int) -> bytes:
            chunk = self._body[self._pos : self._pos + n]
            self._pos += n
            return chunk

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def _open(request, timeout=None):  # noqa: ANN001 - OpenerDirector.open
        url = getattr(request, "full_url", str(request))
        name = url.rsplit("/", 1)[-1]
        if name not in available:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)  # type: ignore[arg-type]
        return _Resp(_POSTER if name.endswith(".jpg") else _CLIP)

    return _open


class TestCachedness:
    def test_both_files_present_and_the_right_size_counts_as_cached(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            assert cache_mod.is_cached(manifest.entries[0], "0.6.0") is False
            _write_media("0.6.0")
            assert cache_mod.is_cached(manifest.entries[0], "0.6.0") is True

    def test_a_truncated_clip_does_not_count(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            _write_media("0.6.0", clip=_CLIP[:10])
            assert cache_mod.is_cached(manifest.entries[0], "0.6.0") is False

    def test_a_missing_poster_alone_does_not_count(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            folder = manifest_mod.ensure_cache_dir("0.6.0")
            (folder / "hosted-clip.mp4").write_bytes(_CLIP)
            cache_mod.record_verified(folder, "hosted-clip.mp4", _CLIP_SHA)
            assert cache_mod.is_cached(manifest.entries[0], "0.6.0") is False

    def test_a_republished_clip_with_the_same_size_is_not_cached(self, tmp_path: Path) -> None:
        """Same basename, same byte count, new sha256: the size check alone would keep the old file."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            old = _parsed()
            assert cache_mod.is_cached(old.entries[0], "0.6.0") is True
            republished = _parsed(entries=[_entry(sha256="ab" * 32)])
            assert republished.entries[0].bytes == old.entries[0].bytes
            assert cache_mod.is_cached(republished.entries[0], "0.6.0") is False

    def test_a_republished_poster_is_not_cached(self, tmp_path: Path) -> None:
        """The poster has no declared size, so the receipt is its only change detector."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            republished = _parsed(entries=[_entry(poster_sha256="cd" * 32)])
            assert cache_mod.is_cached(republished.entries[0], "0.6.0") is False

    def test_files_without_a_receipt_are_not_cached(self, tmp_path: Path) -> None:
        """A file another process wrote — or one installed before its receipt landed — is fetched again."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0", receipts=False)
            assert cache_mod.is_cached(_parsed().entries[0], "0.6.0") is False

    def test_a_malformed_receipt_reads_as_absent(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = _write_media("0.6.0")
            (folder / ".hosted-clip.mp4.sha256").write_text("not a digest\n")
            assert cache_mod.recorded_sha256(folder, "hosted-clip.mp4") == ""
            assert cache_mod.is_cached(_parsed().entries[0], "0.6.0") is False

    def test_the_receipt_is_never_served(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            assert cache_mod.resolve_served_path("0.6.0", ".hosted-clip.mp4.sha256") is None
            assert cache_mod.resolve_served_path("0.6.0", "hosted-clip.mp4.sha256") is None


class TestDownloadPass:
    def test_downloads_poster_and_clip_verified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_media_fetch({"hosted-clip.mp4", "hosted-clip.jpg"})),
        )
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            cache = cache_mod.FeatureVideoCache()
            ok, err = cache._download_entry(manifest.entries[0], manifest, 0)
            assert (ok, err) == (True, "")
            folder = manifest_mod.release_dir("0.6.0")
            assert (folder / "hosted-clip.mp4").read_bytes() == _CLIP
            assert (folder / "hosted-clip.jpg").read_bytes() == _POSTER

    def test_an_unwritable_receipt_fails_the_entry_not_the_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Media landed, receipt could not be written: a failed ENTRY, with a reason, no exception.

        An OSError escaping here would end ``ensure_all`` with the status stuck on
        ``downloading`` and every later entry skipped. Without its receipt the media
        reads as not cached, so the next pass re-fetches it and tries again.
        """
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_media_fetch({"hosted-clip.mp4", "hosted-clip.jpg"})),
        )

        def _disk_full(*_a: object, **_k: object) -> None:
            raise OSError(28, "No space left on device")

        # The receipt is published through the pinned folder handle, so the seam is
        # that handle's write, not a module-level atomic_write.
        monkeypatch.setattr(asset_downloader.PinnedTargetDir, "write_text", _disk_full)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            cache = cache_mod.FeatureVideoCache()
            ok, err = cache._download_entry(manifest.entries[0], manifest, 0)
            assert (ok, err) == (False, cache_mod.RECEIPT_FAILED)
            assert cache_mod.is_cached(manifest.entries[0], "0.6.0") is False

    def test_a_missing_poster_fails_before_the_clip_transfer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_media_fetch({"hosted-clip.mp4"})),
        )
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            cache = cache_mod.FeatureVideoCache()
            ok, _err = cache._download_entry(manifest.entries[0], manifest, 0)
            assert ok is False
            assert not (manifest_mod.release_dir("0.6.0") / "hosted-clip.mp4").exists()

    def test_a_wrong_sha_leaves_nothing_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_media_fetch({"hosted-clip.mp4", "hosted-clip.jpg"})),
        )
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed(entries=[_entry(sha256="0" * 64)])
            cache = cache_mod.FeatureVideoCache()
            ok, err = cache._download_entry(manifest.entries[0], manifest, 0)
            assert ok is False and "sha256 mismatch" in err
            assert not (manifest_mod.release_dir("0.6.0") / "hosted-clip.mp4").exists()

    def test_the_skip_env_makes_the_pass_a_no_op(self, tmp_path: Path) -> None:
        with patch.dict(
            os.environ,
            {"KIROCREW_HOME": str(tmp_path), cache_mod.SKIP_DOWNLOAD_ENV: "1"},
        ):
            assert asyncio.run(cache_mod.feature_video_cache().ensure_all()) is False
            assert cache_mod.start_background_feature_video_download() is None

    def test_the_kill_switch_stops_the_pass_before_any_governance_probe(
        self, tmp_path: Path
    ) -> None:
        with patch.dict(
            os.environ, {"KIROCREW_HOME": str(tmp_path), cache_mod.SKIP_DOWNLOAD_ENV: ""}
        ):
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = False
            cache = cache_mod.feature_video_cache()
            with (
                patch("kiro_crew.feature_videos_cache.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", side_effect=AssertionError),
            ):
                cfg_cls.load.return_value = cfg
                assert asyncio.run(cache.ensure_all()) is False
            assert cache.status["download_state"] == cache_mod.STATE_DISABLED

    def test_a_denied_ceiling_makes_no_request_and_reports_denied(self, tmp_path: Path) -> None:
        with patch.dict(
            os.environ, {"KIROCREW_HOME": str(tmp_path), cache_mod.SKIP_DOWNLOAD_ENV: ""}
        ):
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = True
            cache = cache_mod.feature_video_cache()
            with (
                patch("kiro_crew.feature_videos_cache.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", return_value=True),
                patch.object(
                    manifest_mod, "fetch_manifest", side_effect=AssertionError("fetched anyway")
                ),
            ):
                cfg_cls.load.return_value = cfg
                assert asyncio.run(cache.ensure_all()) is False
            assert cache.status["download_state"] == cache_mod.STATE_DENIED


class TestGovernanceDuringAPass:
    """A paced pass runs for minutes, so the ceiling is re-read per clip."""

    def test_turning_the_feature_off_mid_pass_stops_the_remaining_clips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The kill switch is re-read per entry too, not snapshotted before the loop."""
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_media_fetch({"a.mp4", "a.jpg", "b.mp4", "b.jpg", "c.mp4", "c.jpg"})),
        )
        with patch.dict(
            os.environ, {"KIROCREW_HOME": str(tmp_path), cache_mod.SKIP_DOWNLOAD_ENV: ""}
        ):
            manifest = _parsed(entries=[_entry("a"), _entry("b"), _entry("c")])
            cache = cache_mod.feature_video_cache()
            # Enabled for the pre-loop check and entry a; turned off from then on.
            flags = iter([True, True, False, False, False])

            def _cfg() -> MagicMock:
                cfg = MagicMock()
                cfg.dashboard.feature_videos_enabled = next(flags, False)
                cfg.dashboard.feature_videos_cache_max_mb = 500
                return cfg

            with (
                patch("kiro_crew.feature_videos_cache.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", return_value=False),
                patch.object(cache, "refresh_manifest", return_value=manifest),
            ):
                cfg_cls.load.side_effect = _cfg
                assert asyncio.run(cache.ensure_all()) is False
            assert cache.status["download_state"] == cache_mod.STATE_DISABLED
            folder = manifest_mod.release_dir("0.6.0")
            assert (folder / "a.mp4").is_file()
            assert not (folder / "b.mp4").exists()

    def test_a_denial_mid_pass_stops_the_remaining_clips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Checking once before the loop would keep downloading under a lifted grant."""
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_media_fetch({"a.mp4", "a.jpg", "b.mp4", "b.jpg", "c.mp4", "c.jpg"})),
        )
        with patch.dict(
            os.environ, {"KIROCREW_HOME": str(tmp_path), cache_mod.SKIP_DOWNLOAD_ENV: ""}
        ):
            manifest = _parsed(entries=[_entry("a"), _entry("b"), _entry("c")])
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = True
            cfg.dashboard.feature_videos_cache_max_mb = 500
            cache = cache_mod.feature_video_cache()
            # Permit the pre-loop check, then entry a's poster and clip (one
            # audited answer per request); deny from then on.
            answers = iter([False, False, False, True, True, True, True])

            def _denied() -> bool:
                return next(answers, True)

            with (
                patch("kiro_crew.feature_videos_cache.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", side_effect=_denied),
                patch.object(cache, "refresh_manifest", return_value=manifest),
            ):
                cfg_cls.load.return_value = cfg
                assert asyncio.run(cache.ensure_all()) is False
            assert cache.status["download_state"] == cache_mod.STATE_DENIED
            folder = manifest_mod.release_dir("0.6.0")
            # The first clip landed; the pass stopped before the rest.
            assert (folder / "a.mp4").is_file()
            assert not (folder / "c.mp4").exists()

    def test_each_entry_in_flight_is_reported_with_its_own_title(self, tmp_path: Path) -> None:
        """The pass records the title from the entry it is fetching, so the
        status route never pairs one clip's id with another's title."""
        with patch.dict(
            os.environ, {"KIROCREW_HOME": str(tmp_path), cache_mod.SKIP_DOWNLOAD_ENV: ""}
        ):
            manifest = _parsed(entries=[_entry("a", title="Clip A"), _entry("b", title="Clip B")])
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = True
            cfg.dashboard.feature_videos_cache_max_mb = 500
            cache = cache_mod.feature_video_cache()
            seen: list[tuple[object, object]] = []

            def _fake_download(entry: object, _current: object, _rate: int) -> tuple[bool, str]:
                seen.append((cache.status["downloading"], cache.status["downloading_title"]))
                return False, "fake"

            with (
                patch("kiro_crew.feature_videos_cache.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", return_value=False),
                patch.object(cache, "refresh_manifest", return_value=manifest),
                patch.object(cache, "_download_entry", side_effect=_fake_download),
            ):
                cfg_cls.load.return_value = cfg
                asyncio.run(cache.ensure_all())
            assert seen == [("a", "Clip A"), ("b", "Clip B")]
            # Once the pass ends, nothing is in flight and no title lingers.
            assert cache.status["downloading"] is None
            assert cache.status["downloading_title"] is None

    def test_eviction_runs_again_after_the_clips_have_landed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-loop eviction sizes the cache before the new bytes arrive; the
        post-loop one is what holds the budget once they have. Pinned by observing
        the state of the current folder at each eviction call."""
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_media_fetch({"a.mp4", "a.jpg"})),
        )
        with patch.dict(
            os.environ, {"KIROCREW_HOME": str(tmp_path), cache_mod.SKIP_DOWNLOAD_ENV: ""}
        ):
            manifest = _parsed(entries=[_entry("a")])
            cfg = MagicMock()
            cfg.dashboard.feature_videos_enabled = True
            cfg.dashboard.feature_videos_cache_max_mb = 500
            cache = cache_mod.feature_video_cache()
            folder = manifest_mod.release_dir("0.6.0")
            calls: list[tuple[str, bool]] = []

            def _evict(keep_release: str, *, max_bytes: int) -> list[str]:
                calls.append((keep_release, (folder / "a.mp4").is_file()))
                return []

            with (
                patch("kiro_crew.feature_videos_cache.KiroCrewConfig") as cfg_cls,
                patch.object(manifest_mod, "download_denied", return_value=False),
                patch.object(cache, "refresh_manifest", return_value=manifest),
                patch.object(cache_mod, "evict", _evict),
            ):
                cfg_cls.load.return_value = cfg
                assert asyncio.run(cache.ensure_all()) is True
            assert calls == [("0.6.0", False), ("0.6.0", True)]

    def test_the_poster_transfer_carries_a_ceiling(self, tmp_path: Path) -> None:
        """The manifest declares no poster byte count, so the call must bound it itself.

        Without a bound the only disk-fill guard in ``download_to`` never fires and
        an endless poster body fills the disk before the digest can reject it.
        """
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            seen: list[dict] = []

            def _record(*_args: object, **kwargs: object) -> tuple[bool, str]:
                seen.append(kwargs)
                return False, "stopped"

            with patch.object(cache_mod.asset_downloader, "download_to", _record):
                cache_mod.FeatureVideoCache()._download_entry(manifest.entries[0], manifest, 0)
        assert seen, "download_to was never called"
        assert seen[0]["max_bytes"] == cache_mod.MAX_POSTER_BYTES
        assert "size" not in seen[0], "a bound must not masquerade as a declared length"

    def test_a_withdrawal_during_the_poster_stops_the_clip_request(self, tmp_path: Path) -> None:
        """Each outbound request takes its own audited answer, not the previous one's."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            labels: list[str] = []

            def _record(*_args: object, **kwargs: object) -> tuple[bool, str]:
                labels.append(str(kwargs.get("label")))
                return True, ""

            with (
                patch.object(cache_mod.asset_downloader, "download_to", _record),
                patch.object(manifest_mod, "download_denied", return_value=True),
            ):
                ok, err = cache_mod.FeatureVideoCache()._download_entry(
                    manifest.entries[0], manifest, 0
                )
        assert (ok, err) == (False, cache_mod.DENIED_MID_ENTRY)
        assert labels == ["feature-video poster hosted-clip"], "the clip GET must not be made"


class TestGovernanceReadSplit:
    """An audited evaluation belongs at the action; a polled read gets the memo."""

    def test_the_cached_read_evaluates_once_then_serves_the_memo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def _denied() -> bool:
            calls.append(1)
            return False

        monkeypatch.setattr(manifest_mod, "download_denied", _denied)
        monkeypatch.setattr(manifest_mod, "_last_download_check_ts", 0.0)
        monkeypatch.setattr(manifest_mod, "_last_download_permitted", True)
        for _ in range(25):
            assert manifest_mod.download_permitted_cached() is True
        assert len(calls) == 1, "a polled route must not write one SEL row per poll"

    def test_the_cached_read_re_evaluates_after_the_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []
        monkeypatch.setattr(manifest_mod, "download_denied", lambda: bool(calls.append(1)))
        monkeypatch.setattr(manifest_mod, "_last_download_check_ts", 0.0)
        manifest_mod.download_permitted_cached()
        monkeypatch.setattr(
            manifest_mod, "_last_download_check_ts", -manifest_mod._GOVERNANCE_TTL_SECS
        )
        manifest_mod.download_permitted_cached()
        assert len(calls) == 2

    def test_a_cold_process_evaluates_rather_than_reporting_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Serving the fail-closed default here would tell the user the feature is off."""
        monkeypatch.setattr(manifest_mod, "_last_download_check_ts", 0.0)
        monkeypatch.setattr(manifest_mod, "_last_download_permitted", False)
        monkeypatch.setattr(manifest_mod, "download_denied", lambda: False)
        assert manifest_mod.download_permitted_cached() is True
