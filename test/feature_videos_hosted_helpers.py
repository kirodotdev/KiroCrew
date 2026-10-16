"""Shared fixtures and builders for the hosted feature-video suite
(``test_feature_videos_hosted.py`` and its ``test_feature_videos_hosted_<topic>.py``
parts): the clip and poster bytes, the manifest entry and document builders, seeding
the cache singleton, simulating an installed entry, and the two autouse fixtures every
part runs under -- a fresh cache singleton, and no network.
"""

from __future__ import annotations

import hashlib
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import feature_videos_cache as cache_mod
from kiro_crew import feature_videos_manifest as manifest_mod

_CLIP = b"clip-bytes" * 64
_POSTER = b"poster-bytes" * 8
_CLIP_SHA = hashlib.sha256(_CLIP).hexdigest()
_POSTER_SHA = hashlib.sha256(_POSTER).hexdigest()
_CDN = "https://cdn.example.com/feature-videos"


@pytest.fixture(autouse=True)
def _fresh_cache() -> "object":
    cache_mod.reset_feature_video_cache()
    yield None
    cache_mod.reset_feature_video_cache()


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the suite's network block, moved verbatim; no test in this suite calls monkeypatch.undo()
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that forgets to install a fake must fail, never reach the network."""

    def _blocked(*_a: object, **_k: object) -> None:
        raise urllib.error.URLError("blocked by test fixture")

    # build_opener is the one seam both clients share (the manifest fetch and the
    # media transfer route through it, so they cannot differ on redirect policy).
    # urlopen stays blocked too, so a test that reaches for the old path fails
    # rather than escaping to the network. feature_videos.py itself imports no
    # urllib at all — it makes no outbound request.
    monkeypatch.setattr(
        "kiro_crew.asset_downloader.build_opener",
        lambda *a, **k: SimpleNamespace(open=_blocked),
    )
    for module in ("kiro_crew.feature_videos_manifest", "kiro_crew.asset_downloader"):
        monkeypatch.setattr(f"{module}.urllib.request.urlopen", _blocked)


def _entry(video_id: str = "hosted-clip", **overrides: object) -> dict:
    row: dict = {
        "id": video_id,
        "feature": video_id,
        "title": "A hosted clip",
        "description": "One or two plain sentences.",
        "file": f"{video_id}.mp4",
        "poster": f"{video_id}.jpg",
        "sha256": _CLIP_SHA,
        "poster_sha256": _POSTER_SHA,
        "bytes": len(_CLIP),
        "duration_s": 18.0,
        "doc": "feature-tips.md",
        "used_when": [],
        "min_version": "",
    }
    row.update(overrides)
    return row


def _document(
    release: str = "0.6.0", entries: "list[dict] | None" = None, **overrides: object
) -> dict:
    doc: dict = {
        "schema": manifest_mod.MANIFEST_SCHEMA,
        "release": release,
        "cdn_base": _CDN,
        "generated_at": "2026-09-10T00:00:00Z",
        "entries": entries if entries is not None else [_entry()],
    }
    doc.update(overrides)
    return doc


def _seed(manifest: manifest_mod.VideoManifest) -> manifest_mod.VideoManifest:
    """Put *manifest* straight into the cache singleton's memory.

    Bypasses the loader on purpose: the selection tests are about what happens
    once a manifest is in force, and ``TestManifestOnDisk`` covers how one gets
    there (including that it is re-verified on the way).
    """
    cache_mod.feature_video_cache()._manifest = manifest
    return manifest


def _parsed(
    release: str = "0.6.0", entries: "list[dict] | None" = None
) -> manifest_mod.VideoManifest:
    parsed = manifest_mod.parse_manifest(_document(release=release, entries=entries))
    assert parsed is not None
    return parsed


def _write_media(
    release: str,
    entry_id: str = "hosted-clip",
    *,
    clip: bytes = _CLIP,
    clip_sha: str = _CLIP_SHA,
    poster_sha: str = _POSTER_SHA,
    receipts: bool = True,
) -> Path:
    """Simulate an installed entry: both files plus, by default, both receipts."""
    folder = manifest_mod.ensure_cache_dir(release)
    (folder / f"{entry_id}.mp4").write_bytes(clip)
    (folder / f"{entry_id}.jpg").write_bytes(_POSTER)
    if receipts:
        cache_mod.record_verified(folder, f"{entry_id}.mp4", clip_sha)
        cache_mod.record_verified(folder, f"{entry_id}.jpg", poster_sha)
    return folder


def _as_opener(open_fn):
    """Wrap a urlopen-shaped fake as a ``build_opener`` replacement.

    Both clients (manifest, media) call ``build_opener`` so they cannot differ on
    redirect policy, which makes it the one seam a test replaces.
    """
    return lambda *args, **kwargs: SimpleNamespace(open=open_fn)
