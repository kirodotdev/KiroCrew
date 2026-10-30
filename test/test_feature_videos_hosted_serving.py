"""Hosted feature videos: the same-origin media route (ranges, HEAD, link swaps refused),
selection and ``/next`` making no request, same-origin source URLs, and which hosted
entries selection offers.
``test_feature_videos_hosted.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from feature_videos_hosted_helpers import _fresh_cache  # noqa: F401 - fixture
from feature_videos_hosted_helpers import _no_network  # noqa: F401 - fixture
from feature_videos_hosted_helpers import _CDN, _CLIP, _entry, _parsed, _seed, _write_media

from kiro_crew import feature_videos as fv
from kiro_crew import feature_videos_cache as cache_mod
from kiro_crew import feature_videos_manifest as manifest_mod

# ── serving ──


def _file_request(
    release: str, name: str, *, headers: "dict[str, str] | None" = None, method: str = "GET"
) -> web.Request:
    """A real aiohttp request over a mocked transport, so the route can prepare and stream."""
    return make_mocked_request(
        method,
        f"/feature-videos/{release}/{name}",
        headers=headers or {},
        match_info={"release": release, "name": name},
    )


def _served_body(request: web.Request) -> bytes:
    """Everything the route wrote to the (mocked) transport."""
    return b"".join(call.args[0] for call in request._payload_writer.write.call_args_list)


@pytest.fixture(autouse=True)
def _default_on_intro_already_retired(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the bundled default-on intro out of the pool.

    A bundled ``default_on`` entry (the Mates intro) joins the pool beside a
    hosted manifest by design, and it ships no media, so the asset gate does
    not withhold it in a test tree with no ``static/dist``. This module is
    about hosted clips, so the intro is retired the way it would be on a
    machine that already saw it; ``test_feature_videos.py`` covers the intro
    riding beside a manifest.
    """
    _floor_monkeypatch.setitem(fv._PROBES, "crewmates_onboarded", lambda: True)


class TestServingRoute:
    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privilege")
    def test_a_symlinked_cache_root_serves_nothing(self, tmp_path: Path) -> None:
        """Read, write and delete share one anchor: a linked root is refused by all three."""
        outside = tmp_path / "elsewhere"
        (outside / "0.6.0").mkdir(parents=True)
        (outside / "0.6.0" / "hosted-clip.mp4").write_bytes(b"x")
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest_mod.cache_root().parent.mkdir(parents=True, exist_ok=True)
            manifest_mod.cache_root().symlink_to(outside, target_is_directory=True)
            assert cache_mod.resolve_served_path("0.6.0", "hosted-clip.mp4") is None

    @pytest.mark.parametrize("method", ["GET", "HEAD"])
    def test_the_media_route_is_never_answered_with_the_spa_shell(self, method: str) -> None:
        """A data namespace: a cold-start request gets the auth refusal, not index.html.

        The shell fallback answers UNAUTHENTICATED GETs so the token bootstrap can
        load. A <video> pointed at a clip must not receive HTML with a 200 —
        the exclusion list is what keeps this route on the refusing side.
        """
        import kiro_crew.dashboard.token_auth as ta

        request = MagicMock()
        request.method = method
        request.path = "/feature-videos/0.6.0/hosted-clip.mp4"
        assert "/feature-videos/" in ta.SPA_FALLBACK_EXCLUDED_PREFIXES
        assert ta._is_spa_shell_request(request) is False

    def test_serves_a_cached_file(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            request = _file_request("0.6.0", "hosted-clip.mp4")
            resp = asyncio.run(cache_mod.api_feature_video_file(request))
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "video/mp4"
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
            assert resp.headers["Accept-Ranges"] == "bytes"
            assert resp.content_length == len(_CLIP)
            assert _served_body(request) == _CLIP

    @pytest.mark.parametrize(
        ("header", "status", "content_range", "expected"),
        [
            ("bytes=10-19", 206, f"bytes 10-19/{len(_CLIP)}", _CLIP[10:20]),
            ("bytes=630-", 206, f"bytes 630-{len(_CLIP) - 1}/{len(_CLIP)}", _CLIP[630:]),
            ("bytes=-5", 206, f"bytes {len(_CLIP) - 5}-{len(_CLIP) - 1}/{len(_CLIP)}", _CLIP[-5:]),
            ("bytes=0-99999", 206, f"bytes 0-{len(_CLIP) - 1}/{len(_CLIP)}", _CLIP),
        ],
    )
    def test_a_range_request_is_honoured_so_the_player_can_seek(
        self, tmp_path: Path, header: str, status: int, content_range: str, expected: bytes
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            request = _file_request("0.6.0", "hosted-clip.mp4", headers={"Range": header})
            resp = asyncio.run(cache_mod.api_feature_video_file(request))
            assert resp.status == status
            assert resp.headers["Content-Range"] == content_range
            assert resp.content_length == len(expected)
            assert _served_body(request) == expected

    @pytest.mark.parametrize("header", ["bytes=99999-", "bytes=abc", "bytes=5-3", "items=0-1"])
    def test_an_unsatisfiable_or_malformed_range_is_a_416(
        self, tmp_path: Path, header: str
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            with pytest.raises(web.HTTPRequestRangeNotSatisfiable) as excinfo:
                asyncio.run(
                    cache_mod.api_feature_video_file(
                        _file_request("0.6.0", "hosted-clip.mp4", headers={"Range": header})
                    )
                )
            assert excinfo.value.headers["Content-Range"] == f"bytes */{len(_CLIP)}"

    def test_a_head_request_carries_the_headers_and_no_body(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            request = _file_request("0.6.0", "hosted-clip.mp4", method="HEAD")
            resp = asyncio.run(cache_mod.api_feature_video_file(request))
            assert resp.status == 200
            assert resp.content_length == len(_CLIP)
            assert resp.headers["Content-Type"] == "video/mp4"
            assert _served_body(request) == b""

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privilege on Windows")
    def test_a_file_swapped_for_a_link_after_resolution_is_not_followed(
        self, tmp_path: Path
    ) -> None:
        """The check-then-open window: the open itself refuses the link.

        ``resolve_served_path`` lstat-checks a regular file; between that and the
        open, a same-user writer swaps it for a symlink to something the gateway
        can read. A route that reopened by path would follow it. This one opens
        once with no-follow semantics and streams from that descriptor, so the swap
        is a 404 and the target is never read.
        """
        secret = tmp_path / "secret.bin"
        secret.write_bytes(b"not yours")
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = _write_media("0.6.0")
            clip = folder / "hosted-clip.mp4"
            real_resolve = cache_mod.resolve_served_path

            def _resolve_then_swap(release: str, name: str):
                resolved = real_resolve(release, name)
                assert resolved is not None, "the pre-swap check must have passed"
                clip.unlink()
                clip.symlink_to(secret)
                return resolved

            with patch.object(cache_mod, "resolve_served_path", _resolve_then_swap):
                request = _file_request("0.6.0", "hosted-clip.mp4")
                with pytest.raises(web.HTTPNotFound):
                    asyncio.run(cache_mod.api_feature_video_file(request))
            assert _served_body(request) == b""

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privilege on Windows")
    def test_a_folder_swapped_for_a_link_after_resolution_is_not_followed(
        self, tmp_path: Path
    ) -> None:
        """The ancestor swap on the read side.

        The leaf rule cannot see this one: after the resolver's lstat the RELEASE
        FOLDER is renamed away and a link to an outside directory takes its name,
        and the open finds a perfectly ordinary regular file at the end of the
        link. What refuses it is the descriptor's real path, which is not the
        canonical ``<root>/0.6.0/hosted-clip.mp4`` the resolver built.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "hosted-clip.mp4").write_bytes(b"not yours")
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = _write_media("0.6.0")
            real_resolve = cache_mod.resolve_served_path

            def _resolve_then_swap(release: str, name: str):
                resolved = real_resolve(release, name)
                assert resolved is not None, "the pre-swap check must have passed"
                folder.rename(tmp_path / "moved")
                folder.symlink_to(outside, target_is_directory=True)
                return resolved

            with patch.object(cache_mod, "resolve_served_path", _resolve_then_swap):
                request = _file_request("0.6.0", "hosted-clip.mp4")
                with pytest.raises(web.HTTPNotFound):
                    asyncio.run(cache_mod.api_feature_video_file(request))
            assert _served_body(request) == b""

    def test_a_hard_link_into_the_cache_is_not_served(self, tmp_path: Path) -> None:
        """A second name for an outside inode passes every path rule; the link count does not."""
        secret = tmp_path / "secret.bin"
        secret.write_bytes(b"not yours")
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = _write_media("0.6.0")
            try:
                os.link(secret, folder / "alias.mp4")
            except OSError as exc:  # pragma: no cover - a filesystem without hard links
                pytest.skip(f"hard links unavailable here: {exc}")
            assert cache_mod.resolve_served_path("0.6.0", "alias.mp4") is not None
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(cache_mod.api_feature_video_file(_file_request("0.6.0", "alias.mp4")))

    def test_a_descriptor_whose_real_path_cannot_be_read_is_a_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            monkeypatch.setattr(cache_mod.pinned_fs, "fd_real_path", lambda _fd: None)
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(
                    cache_mod.api_feature_video_file(_file_request("0.6.0", "hosted-clip.mp4"))
                )

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink semantics")
    def test_a_root_swapped_for_a_link_inside_the_resolve_window_is_a_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The GPT shape: the root passes ``_refuse_link`` and is then, before the
        by-name ``resolve``, replaced by a link to a folder outside the cache that
        holds a same-named release and clip. The resolver's canonical path is now
        the outside folder — and the open does not trust it: root, release and file
        are opened as a no-follow chain from the root's own name, so the link is
        refused and the outside clip is never served."""
        outside = tmp_path / "outside"
        (outside / "0.6.0").mkdir(parents=True)
        (outside / "0.6.0" / "hosted-clip.mp4").write_bytes(b"outside bytes")
        real_refuse = manifest_mod._refuse_link
        swapped: list[Path] = []

        def _check_then_swap_root(path: Path, label: str) -> None:
            real_refuse(path, label)
            if "root" in label and not swapped:
                shutil.move(str(path), str(tmp_path / "moved-away"))
                path.symlink_to(outside, target_is_directory=True)
                swapped.append(path)

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            monkeypatch.setattr(manifest_mod, "_refuse_link", _check_then_swap_root)
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(
                    cache_mod.api_feature_video_file(_file_request("0.6.0", "hosted-clip.mp4"))
                )
        assert swapped, "the swap seam never fired"

    def test_a_file_swapped_for_a_directory_after_resolution_is_a_404(self, tmp_path: Path) -> None:
        """Same window, non-regular leaf: fstat on the opened descriptor refuses it."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = _write_media("0.6.0")
            clip = folder / "hosted-clip.mp4"
            real_resolve = cache_mod.resolve_served_path

            def _resolve_then_swap(release: str, name: str):
                resolved = real_resolve(release, name)
                clip.unlink()
                clip.mkdir()
                return resolved

            with patch.object(cache_mod, "resolve_served_path", _resolve_then_swap):
                with pytest.raises(web.HTTPNotFound):
                    asyncio.run(
                        cache_mod.api_feature_video_file(_file_request("0.6.0", "hosted-clip.mp4"))
                    )

    @pytest.mark.parametrize(
        ("name", "content_type"),
        [
            ("hosted-clip.mp4", "video/mp4"),
            ("hosted-clip.jpg", "image/jpeg"),
            ("poster.jpeg", "image/jpeg"),
            ("poster.png", "image/png"),
            ("poster.webp", "image/webp"),
            ("LOUD.MP4", "video/mp4"),
        ],
    )
    def test_the_content_type_comes_from_the_suffix_table_not_the_bytes(
        self, tmp_path: Path, name: str, content_type: str
    ) -> None:
        """The type is decided by the name the parser admitted, never sniffed."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.ensure_cache_dir("0.6.0")
            (folder / name).write_bytes(b"<html><script>alert(1)</script></html>")
            resp = asyncio.run(cache_mod.api_feature_video_file(_file_request("0.6.0", name)))
            assert resp.headers["Content-Type"] == content_type
            assert resp.headers["X-Content-Type-Options"] == "nosniff"

    @pytest.mark.parametrize(
        "name",
        [
            "planted.html",
            "planted.htm",
            "planted.svg",
            "planted.js",
            "planted.xml",
            "planted.txt",
            "planted.json",
            "planted",
            "manifest.json",
            "hosted-clip.mp4.html",
            "hosted-clip.html.mp4x",
        ],
    )
    def test_a_name_the_parser_could_not_have_admitted_is_never_served(
        self, tmp_path: Path, name: str
    ) -> None:
        """The folder is agent-writable; a planted file of any other type is not reachable.

        A regular file IS on disk under that name, so this is the suffix rule
        refusing, not containment: without it a planted ``x.html`` would come back
        on the dashboard's own origin as something the browser executes.
        """
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.ensure_cache_dir("0.6.0")
            (folder / name).write_bytes(b"<html><script>alert(1)</script></html>")
            assert cache_mod.resolve_served_path("0.6.0", name) is None
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(cache_mod.api_feature_video_file(_file_request("0.6.0", name)))

    def test_the_served_suffixes_are_exactly_the_parsers(self) -> None:
        """One rule: whatever the parser admits for a clip or a poster, and nothing else."""
        admitted = {manifest_mod._CLIP_SUFFIX, *manifest_mod._POSTER_SUFFIXES}
        assert set(manifest_mod._SERVED_CONTENT_TYPES) == admitted
        for suffix in admitted:
            assert manifest_mod.served_content_type(f"x{suffix}")
        assert manifest_mod.served_content_type("x.mp4.bak") == ""
        assert manifest_mod.served_content_type(".mp4") == ""
        assert manifest_mod.served_content_type(None) == ""

    @pytest.mark.parametrize(
        ("release", "name"),
        [
            ("0.6.0", "../../../etc/passwd"),
            ("0.6.0", "..%2fsecret"),
            ("0.6.0", "sub/clip.mp4"),
            ("0.6.0", "clip.mp4%00.txt"),
            ("0.6.0", "\\clip.mp4"),
            ("0.6.0", "/etc/passwd"),
            ("0.6.0", "C:clip.mp4"),
            ("0.6.0", ""),
            ("0.6.0", "."),
            ("0.6.0", ".."),
            ("../0.6.0", "hosted-clip.mp4"),
            ("0.6.0/../..", "hosted-clip.mp4"),
            ("latest", "hosted-clip.mp4"),
            ("", "hosted-clip.mp4"),
        ],
    )
    def test_an_unsafe_component_is_a_flat_404(
        self, tmp_path: Path, release: str, name: str
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(cache_mod.api_feature_video_file(_file_request(release, name)))

    def test_a_file_that_is_not_there_is_a_404(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest_mod.ensure_cache_dir("0.6.0")
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(cache_mod.api_feature_video_file(_file_request("0.6.0", "gone.mp4")))

    def test_a_directory_is_never_served(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.ensure_cache_dir("0.6.0")
            (folder / "sub.mp4").mkdir()
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(cache_mod.api_feature_video_file(_file_request("0.6.0", "sub.mp4")))

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privilege on Windows")
    def test_a_symlink_out_of_the_cache_is_refused(self, tmp_path: Path) -> None:
        """Name validation cannot see a symlink; the containment re-check can."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.ensure_cache_dir("0.6.0")
            secret = tmp_path / "secret.mp4"
            secret.write_bytes(b"not yours")
            (folder / "escape.mp4").symlink_to(secret)
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(cache_mod.api_feature_video_file(_file_request("0.6.0", "escape.mp4")))

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privilege on Windows")
    def test_a_symlinked_release_folder_cannot_become_the_boundary(self, tmp_path: Path) -> None:
        """The escape one level UP from the file: the release folder is the link.

        Anchoring containment to the release folder makes this pass every check —
        the folder resolves to the outside directory, and a file in that directory
        is then "inside" the root. Anchoring to the cache root is what refuses it.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "hosted-clip.mp4").write_bytes(b"not yours")
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            root = manifest_mod.cache_root()
            root.mkdir(parents=True, exist_ok=True)
            (root / "0.6.0").symlink_to(outside, target_is_directory=True)
            assert cache_mod.resolve_served_path("0.6.0", "hosted-clip.mp4") is None
            with pytest.raises(web.HTTPNotFound):
                asyncio.run(
                    cache_mod.api_feature_video_file(_file_request("0.6.0", "hosted-clip.mp4"))
                )

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privilege on Windows")
    def test_a_symlink_inside_the_cache_is_refused_too(self, tmp_path: Path) -> None:
        """Refused for being a link, not for where it points.

        A manifest names files, so a link has no legitimate reader even when its
        target is a clip in the same folder. Checking containment alone would
        serve this one.
        """
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = _write_media("0.6.0")
            (folder / "alias.mp4").symlink_to(folder / "hosted-clip.mp4")
            assert cache_mod.resolve_served_path("0.6.0", "alias.mp4") is None

    def test_a_missing_cache_root_is_a_404_not_a_crash(self, tmp_path: Path) -> None:
        """Nothing fetched yet: the anchor cannot be resolved, so there is no file."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            assert cache_mod.resolve_served_path("0.6.0", "hosted-clip.mp4") is None


class TestNoEgressFromSelection:
    """Selection and /next never make a request: every offer is on disk.

    There is deliberately no remote-src path — a CDN url in a ``src`` would have
    the BROWSER fetch bytes the sha256 pin never checked and follow redirects the
    gateway's own opener refuses. The no-network fixture above turns any attempt
    into a URLError, so these tests prove the absence by exercising selection
    and the pool under a permit.
    """

    def test_selection_under_a_permit_still_offers_nothing_uncached(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            with patch.object(manifest_mod, "download_denied", return_value=False):
                assert fv.select_next("9.9.9") is None
                assert fv._offer_pool("9.9.9") == ()

    def test_the_module_imports_no_url_client(self) -> None:
        """No urllib, no opener: the module has nothing to reach the network WITH."""
        assert not hasattr(fv, "urllib")
        assert not hasattr(fv, "asset_downloader")
        assert not hasattr(fv, "_probe_remote")

    def test_the_status_readout_uses_the_memo(self, tmp_path: Path) -> None:
        """A display field must not spend an audited decision."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit",
                return_value=SimpleNamespace(permitted=True),
            ) as vet:
                manifest_mod.download_permitted_cached()
                before = vet.call_count
                manifest_mod.download_permitted_cached()
                assert vet.call_count == before


# ── source selection ──


class TestSourceUrlValidation:
    """Same-origin only. The manifest's own CDN host is refused like any other."""

    @pytest.mark.parametrize(
        "url",
        [
            f"{_CDN}/0.6.0/clip.mp4",
            "https://attacker.example/feature-videos/0.6.0/clip.mp4",
            "https://user:pw@cdn.example.com/0.6.0/clip.mp4",
            "https://cdn.example.com/0.6.0/clip.mp4?X-Amz-Signature=abc",
            "http://cdn.example.com/0.6.0/clip.mp4",
            "//cdn.example.com/0.6.0/clip.mp4",
            "data:video/mp4;base64,AAAA",
        ],
    )
    def test_every_off_origin_url_is_refused(self, url: str) -> None:
        assert fv.validate_asset_path(url) == ""

    def test_the_cache_prefix_is_same_origin(self) -> None:
        path = "/feature-videos/0.6.0/clip.mp4"
        assert fv.validate_asset_path(path) == path

    @pytest.mark.parametrize(
        "path",
        [
            "/feature-videos/",
            "/feature-videos/0.6.0/../secret",
            "/feature-videos//0.6.0/clip.mp4",
            "/feature-videos/0.6.0/clip%2e%2e.mp4",
        ],
    )
    def test_an_unsafe_cache_path_is_refused(self, path: str) -> None:
        assert fv.validate_asset_path(path) == ""


class TestHostedSelection:
    def test_a_cached_entry_is_offered_from_this_origin(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            _seed(_parsed())
            picked = fv.select_next("9.9.9")
            assert picked is not None
            assert "source" not in picked.payload(), "a field that could only say local"
            assert picked.src == "/feature-videos/0.6.0/hosted-clip.mp4"
            assert picked.poster == "/feature-videos/0.6.0/hosted-clip.jpg"

    def test_an_uncached_entry_is_not_offered_at_all(self, tmp_path: Path) -> None:
        """Not from the CDN, not from anywhere: the browser never plays unverified bytes."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            assert fv.select_next("9.9.9") is None

    def test_only_the_cached_entries_of_a_mixed_manifest_are_offered(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0", "cached-one")
            _seed(_parsed(entries=[_entry("uncached-one"), _entry("cached-one")]))
            for _ in range(20):
                picked = fv.select_next("9.9.9")
                assert picked is not None
                assert picked.id == "cached-one"

    def test_a_denied_ceiling_still_plays_what_is_on_disk(self, tmp_path: Path) -> None:
        """Withdrawing bytes already downloaded is a separate decision the row does not make."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            with patch.object(manifest_mod, "download_denied", return_value=True):
                assert fv.select_next("9.9.9") is None
            _write_media("0.6.0")
            with patch.object(manifest_mod, "download_denied", return_value=True):
                picked = fv.select_next("9.9.9")
            assert picked is not None and picked.src.startswith(cache_mod.SERVE_PREFIX)

    def test_a_manifest_replaces_the_static_catalog(self, tmp_path: Path) -> None:
        """Mixing them would offer a bundled clip and its hosted successor as two."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.6.0")
            _seed(_parsed())
            with patch.object(fv, "_asset_exists", lambda _p: True):
                for _ in range(20):
                    picked = fv.select_next("9.9.9")
                    assert picked is not None and picked.id == "hosted-clip"

    def test_the_static_catalog_serves_when_no_manifest_was_ever_fetched(
        self, tmp_path: Path
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with patch.object(fv, "_asset_exists", lambda _p: True):
                picked = fv.select_next("9.9.9")
            assert picked is not None
            assert picked.id in {e.id for e in fv.CATALOG}
            assert picked.src.startswith(fv.ASSET_PREFIX)

    def test_a_manifest_with_nothing_landed_yet_offers_nothing_that_launch(
        self, tmp_path: Path
    ) -> None:
        """The stated trade: one quiet launch, never a fall-back to the bundled set.

        Falling back would offer a bundled clip and its hosted successor across two
        launches, and a verdict on one would not retire the other — the same reason
        the manifest replaces the catalog instead of extending it.
        """
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            with patch.object(fv, "_asset_exists", lambda _p: True):
                assert fv.select_next("9.9.9") is None

    def test_recorded_state_and_probes_still_withdraw_a_hosted_entry(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed(entries=[_entry("recorded"), _entry("probed", used_when=["always"])]))
            fv.save_state(fv.FeatureVideoState(videos={"recorded": {"status": "seen", "ts": 1.0}}))
            with patch.dict(fv._PROBES, {"always": lambda: True}):
                assert fv.select_next("9.9.9") is None

    def test_a_version_floor_withdraws_a_hosted_entry(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed(entries=[_entry("future", min_version="99.0.0")]))
            assert fv.select_next("1.2.3") is None

    def test_a_hosted_id_can_record_a_verdict(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _seed(_parsed())
            ids = fv.known_video_ids()
            # Both catalogs: a clip shown before the manifest landed must stay
            # retirable, and its recorded id is what keeps it retired.
            assert "hosted-clip" in ids
            assert {e.id for e in fv.CATALOG} <= ids
