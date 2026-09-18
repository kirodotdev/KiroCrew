"""Tests for the HOSTED half of feature videos: signed manifest, cache, serving.

Signatures here are real. Keys and signing go through
``test/feature_video_fixture.py``, which mints a throwaway RSA pair per test and
repoints ``feed_trust``'s pins at it, so the positive path is exercised with genuine
openssl verification rather than a stubbed "it verified" — a stub would pass for a
manifest nobody signed, which is the one failure this module exists to prevent.

:class:`TestSharedFixture` additionally verifies the COMMITTED fixture at
``test/fixtures/feature-videos/manifest.json``. That artifact is the cross-tool
contract: the publishing tool's tests verify the same bytes, so the signed byte
format is pinned in one place instead of in two helpers that can drift.

Network is faked throughout: ``asset_downloader.build_opener`` is replaced per
test — the one seam the manifest fetch and the media transfer share.
``feature_videos`` itself makes no request at all.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import feature_video_fixture as fixture
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import asset_downloader
from kiro_crew import feature_videos as fv
from kiro_crew import feature_videos_cache as cache_mod
from kiro_crew import feature_videos_manifest as manifest_mod
from kiro_crew import pinned_fs
from kiro_crew.platform import feed_trust

_CLIP = b"clip-bytes" * 64
_POSTER = b"poster-bytes" * 8
_CLIP_SHA = hashlib.sha256(_CLIP).hexdigest()
_POSTER_SHA = hashlib.sha256(_POSTER).hexdigest()
_CDN = "https://cdn.example.com/feature-videos"


# ── fixtures ──


@pytest.fixture(autouse=True)
def _fresh_cache() -> "object":
    cache_mod.reset_feature_video_cache()
    yield None
    cache_mod.reset_feature_video_cache()


@pytest.fixture(autouse=True)
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


@pytest.fixture(scope="session")
def _throwaway_pair(tmp_path_factory: pytest.TempPathFactory) -> "tuple[Path, Path]":
    """One throwaway RSA pair for the whole session.

    Session-scoped because a 3072-bit keygen is ~0.5s and a dozen tests want a
    signing key: minting per test spent most of this file's runtime on openssl.
    The PINNING stays per-test (below), so no test inherits another's patch.
    """
    return fixture.mint_throwaway_key(tmp_path_factory.mktemp("fv-signing-key"))


@pytest.fixture()
def signing_key(_throwaway_pair: "tuple[Path, Path]", monkeypatch: pytest.MonkeyPatch) -> Path:
    """The session key, with ``feed_trust``'s pins repointed at it for this test.

    Both halves come from ``feature_video_fixture``, which is also what the
    publishing tool's tests use — one helper, so a key minted here and a key minted
    there cannot differ in a way that hides an encoding disagreement.
    """
    private, public = _throwaway_pair
    fixture.pin_fixture_key(monkeypatch, public)
    return private


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


def _sign(private: Path, tmp_path: Path, document: dict) -> dict:
    """Attach a real signature over the canonical payload."""
    return fixture.sign_document(private, document, tmp_path)


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


class TestSharedFixture:
    """The committed cross-tool fixture: ``test/fixtures/feature-videos/``.

    The publishing tool's tests verify these same bytes, so what is pinned here is
    the signed byte FORMAT rather than any authority. The private half is minted on
    demand instead of committed — see the header of
    ``test/fixtures/feature-videos/regenerate.py``.
    """

    @pytest.fixture()
    def pinned(self, monkeypatch: pytest.MonkeyPatch) -> str:
        return fixture.pin_fixture_key(monkeypatch)

    def test_the_committed_fixture_verifies_and_parses(self, pinned: str) -> None:
        manifest = manifest_mod.verified_manifest(fixture.load_fixture_manifest())
        assert manifest is not None
        assert manifest.release == "0.6.0"
        assert [e.id for e in manifest.entries] == ["fixture-clip", "fixture-clip-2"]
        assert manifest.cdn_base == "https://cdn.example.invalid/feature-videos"

    def test_the_non_ascii_entry_survives_the_round_trip(self, pinned: str) -> None:
        """The case that separates an ASCII-escaped canonical form from a UTF-8 one.

        Two tools that disagree on this produce a document that verifies on neither
        side — but ONLY for entries carrying non-ASCII text, so a fixture without
        one would look fine while the encodings differed.
        """
        manifest = manifest_mod.verified_manifest(fixture.load_fixture_manifest())
        assert manifest is not None
        assert "日本語" in manifest.entries[1].title

    def test_the_stored_indentation_is_not_what_is_signed(
        self, pinned: str, tmp_path: Path
    ) -> None:
        """Re-serializing the file changes its bytes and not its validity.

        The signature covers the CANONICAL form, so a publisher may write the file
        however it likes. Worth pinning: a verifier that hashed the file as stored
        would pass this fixture and fail every real release.
        """
        loaded = fixture.load_fixture_manifest()
        reserialized = json.loads(json.dumps(loaded, indent=8, sort_keys=False))
        assert json.dumps(reserialized) != json.dumps(loaded, indent=2, sort_keys=True)
        assert manifest_mod.verify_manifest(reserialized) is True

    def test_tampering_with_the_fixture_fails(self, pinned: str) -> None:
        doctored = fixture.load_fixture_manifest()
        doctored["cdn_base"] = "https://attacker.example/videos"
        assert manifest_mod.verify_manifest(doctored) is False

    def test_the_helpers_canonical_bytes_match_the_verifier(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        """The helper's local canonicalization is the verifier's, byte for byte.

        The helper deliberately does NOT import the verifier's copy: signing with
        the same function that verifies cannot detect the two disagreeing, which is
        the failure the whole fixture exists to catch. So the agreement is asserted
        instead — a signature the helper produced verifies, and one produced over
        any other byte form does not.
        """
        document = _document(entries=[_entry("canon", title="Ünïcödé")])
        assert manifest_mod.verify_manifest(_sign(signing_key, tmp_path, document)) is True
        # A UTF-8, unsorted, pretty-printed payload is the exact mistake a second
        # implementation makes; the same key over those bytes must not verify.
        wrong = tmp_path / "wrong.json"
        wrong.write_bytes(
            json.dumps(document, sort_keys=False, indent=2, ensure_ascii=False).encode("utf-8")
        )
        signature = subprocess.run(
            [fixture.openssl_or_skip(), "dgst", "-sha256", "-sign", str(signing_key), str(wrong)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout
        mis_signed = {**document, "signature": base64.b64encode(signature).decode("ascii")}
        assert manifest_mod.verify_manifest(mis_signed) is False

    def test_the_fixture_key_is_not_the_production_trust_root(self) -> None:
        """A test key must never be able to become the thing that grants trust.

        Asserted WITHOUT the pinning fixture, against the real committed pins: if
        someone ever pasted this key into ``feed_trust``, every fixture-signed
        document would verify on a shipped build.
        """
        assert fixture.key_id_of(fixture.PUBLIC_KEY_PATH) != feed_trust.PINNED_KEY_ID
        pem = fixture.PUBLIC_KEY_PATH.read_bytes()
        assert base64.b64encode(pem).decode("ascii") != feed_trust.PINNED_PUBLIC_KEY_B64

    def test_the_fixture_carries_no_private_key(self) -> None:
        """The SAST secrets gate is not something to add an exclusion for."""
        for path in fixture.FIXTURE_DIR.iterdir():
            assert "PRIVATE KEY" not in path.read_text(encoding="utf-8", errors="ignore")


# ── verification ──


class TestManifestVerification:
    def test_a_correctly_signed_manifest_verifies_and_parses(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        signed = _sign(signing_key, tmp_path, _document())
        manifest = manifest_mod.verified_manifest(signed)
        assert manifest is not None
        assert manifest.release == "0.6.0"
        assert [e.id for e in manifest.entries] == ["hosted-clip"]

    def test_tampering_with_one_entry_discards_the_WHOLE_manifest(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        """The signature covers the document, so a doctored entry invalidates all of it."""
        signed = _sign(signing_key, tmp_path, _document())
        signed["entries"][0]["sha256"] = "1" * 64
        assert manifest_mod.verify_manifest(signed) is False
        assert manifest_mod.verified_manifest(signed) is None

    def test_an_unsigned_manifest_is_refused(self, tmp_path: Path) -> None:
        assert manifest_mod.verified_manifest(_document()) is None

    def test_a_signature_from_another_key_is_refused(
        self, signing_key: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        other, _public = fixture.mint_throwaway_key(other_dir)
        assert manifest_mod.verify_manifest(_sign(other, tmp_path, _document())) is False

    def test_a_key_id_naming_another_key_is_refused(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        """key_id is optional, but a value that disagrees with the pin is a lie."""
        assert (
            manifest_mod.verify_manifest(
                _sign(signing_key, tmp_path, _document(key_id="sha256:" + "0" * 64))
            )
            is False
        )

    def test_a_matching_key_id_is_accepted_inside_the_signed_payload(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        signed = _sign(signing_key, tmp_path, _document(key_id=feed_trust.PINNED_KEY_ID))
        assert manifest_mod.verify_manifest(signed) is True

    def test_the_cli_feed_shape_is_not_interchangeable(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        """One key, two documents: each consumer refuses the other's schema."""
        signed = _sign(signing_key, tmp_path, _document())
        # Signature is valid, but the strict CLI-feed entry point still refuses it
        # (nested payload, no key_id) — and the video consumer refuses a feed schema.
        assert feed_trust.verify_manifest_signature(signed) is False
        feed_shaped = _sign(
            signing_key,
            tmp_path,
            {"schema": "kirocrew-cli-artifact-manifest-v1", "channel": "stable"},
        )
        assert manifest_mod.verify_manifest(feed_shaped) is True
        assert manifest_mod.verified_manifest(feed_shaped) is None

    def test_an_oversized_payload_is_refused(self, signing_key: Path, tmp_path: Path) -> None:
        """Over the cap is refused, not truncated — and the cap is the publisher's."""
        big = _document(entries=[_entry(f"clip-{i}", description="x" * 1500) for i in range(200)])
        assert manifest_mod.verify_manifest(_sign(signing_key, tmp_path, big)) is False


# ── structural parsing ──


class TestManifestParsing:
    @pytest.mark.parametrize(
        "duration",
        [10**310, float("inf"), float("nan"), -1, 10**6, True, "18", None],
    )
    def test_an_unusable_duration_reads_as_unknown_not_a_crash(self, duration: object) -> None:
        """A 310-digit integer overflows float(); json admits Infinity and NaN. None ends the parse."""
        manifest = manifest_mod.parse_manifest(_document(entries=[_entry(duration_s=duration)]))
        assert manifest is not None and len(manifest.entries) == 1
        assert manifest.entries[0].duration_s == 0.0

    def test_a_sane_duration_is_kept(self) -> None:
        manifest = manifest_mod.parse_manifest(_document(entries=[_entry(duration_s=18)]))
        assert manifest is not None and manifest.entries[0].duration_s == 18.0

    def test_a_foreign_schema_is_rejected(self) -> None:
        assert manifest_mod.parse_manifest(_document(schema="something-else")) is None

    @pytest.mark.parametrize(
        "release",
        ["", "latest", "../0.6.0", "0.6.0-rc1", "0.6.0/", "1.2.3.4.5", "0.6.0 ", "-1.0.0"],
    )
    def test_an_unsafe_release_is_rejected(self, release: str) -> None:
        assert manifest_mod.parse_manifest(_document(release=release)) is None

    @pytest.mark.parametrize("release", ["0.6", "1", "0.6.0", "1.2.3.4"])
    def test_every_release_shape_the_publisher_can_emit_is_accepted(self, release: str) -> None:
        """A cap tighter than the publisher's would discard a valid release WHOLE.

        ``scripts/feature-videos/_manifest.py`` accepts a bare numeric version of
        any depth; Kiro Crew's own releases are always three components, and the
        fetch path only ever asks for those.
        """
        parsed = manifest_mod.parse_manifest(_document(release=release))
        assert parsed is not None and parsed.release == release

    @pytest.mark.parametrize("base", ["http://cdn.example.com", "//cdn.example.com", "", 42])
    def test_a_non_https_cdn_base_is_rejected(self, base: object) -> None:
        assert manifest_mod.parse_manifest(_document(cdn_base=base)) is None

    def test_entries_must_be_a_list(self) -> None:
        assert manifest_mod.parse_manifest(_document(entries={"a": 1})) is None

    def test_the_entry_count_has_a_coherence_bound(self) -> None:
        """A cheap bound well above what the signed-payload cap already allows."""
        too_many = [_entry(f"clip-{i}") for i in range(manifest_mod._MAX_ENTRIES + 1)]
        assert manifest_mod.parse_manifest(_document(entries=too_many)) is None
        just_under = [_entry(f"clip-{i}") for i in range(3)]
        assert manifest_mod.parse_manifest(_document(entries=just_under)) is not None

    @pytest.mark.parametrize(
        "overrides",
        [
            {"file": "../../etc/passwd"},
            {"file": "clip.mp4/../../x.mp4"},
            {"file": "sub/dir/clip.mp4"},
            {"file": "clip.html"},
            {"file": "clip"},
            {"file": ".hidden.mp4"},
            {"poster": "poster.svg"},
            {"poster": "../poster.jpg"},
            {"sha256": "not-a-digest"},
            {"sha256": _CLIP_SHA.upper()},
            {"poster_sha256": ""},
            {"bytes": 0},
            {"bytes": -1},
            {"bytes": True},
            {"bytes": 10**12},
            {"doc": "internal-design-note.md"},
            {"doc": []},
            {"doc": {"path": "feature-tips.md"}},
            {"doc": None},
            {"min_version": "not-a-version"},
            {"id": "has spaces"},
            {"id": "../escape"},
        ],
    )
    def test_an_unsafe_entry_is_dropped(self, overrides: dict) -> None:
        """A bad ENTRY costs one clip; only a bad DOCUMENT costs the manifest."""
        parsed = manifest_mod.parse_manifest(_document(entries=[_entry(**overrides)]))
        assert parsed is not None
        assert parsed.entries == ()

    def test_good_entries_survive_beside_a_bad_one(self) -> None:
        parsed = manifest_mod.parse_manifest(
            _document(entries=[_entry("good"), _entry("bad", sha256="nope"), _entry("also-good")])
        )
        assert parsed is not None
        assert [e.id for e in parsed.entries] == ["good", "also-good"]

    def test_duplicate_ids_keep_only_the_first(self) -> None:
        parsed = manifest_mod.parse_manifest(
            _document(entries=[_entry("dup", title="first"), _entry("dup", title="second")])
        )
        assert parsed is not None
        assert [e.title for e in parsed.entries] == ["first"]

    @pytest.mark.parametrize(
        "second",
        [
            {"file": "one.mp4"},  # same clip name as the first entry
            {"poster": "one.jpg"},  # same poster name
            {"file": "ONE.MP4"},  # case-insensitive filesystems make this the same path
            {"poster": "two.mp4"},  # its own poster collides with its own clip
        ],
    )
    def test_an_entry_reusing_a_basename_is_dropped(self, second: dict) -> None:
        """A basename is a cache path: two entries on one path would overwrite each other.

        Both files would still be sha256-verified, but one offer would then play
        the OTHER entry's bytes, and the size-only ``is_cached`` check cannot tell.
        """
        rows = [
            _entry("one", file="one.mp4", poster="one.jpg"),
            _entry("two", **{"file": "two.mp4", "poster": "two.jpg", **second}),
        ]
        parsed = manifest_mod.parse_manifest(_document(entries=rows))
        assert parsed is not None
        assert [e.id for e in parsed.entries] == ["one"]

    def test_used_when_keeps_only_non_empty_strings(self) -> None:
        parsed = manifest_mod.parse_manifest(
            _document(entries=[_entry(used_when=["tips_feedback_exists", "", 7, None])])
        )
        assert parsed is not None
        assert parsed.entries[0].used_when == ("tips_feedback_exists",)

    def test_asset_url_is_derived_not_declared(self) -> None:
        manifest = _parsed()
        assert manifest.asset_url("x.mp4") == f"{_CDN}/0.6.0/x.mp4"


# ── release resolution ──


class TestReleaseResolution:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("0.6.0", ("0.6.0", "0.5.0", "0.4.0", "0.3.0")),
            ("0.6.3", ("0.6.3", "0.6.0", "0.5.0", "0.4.0", "0.3.0")),
            ("0.6.0rc3", ("0.6.0", "0.5.0", "0.4.0", "0.3.0")),
            ("1.1.0", ("1.1.0", "1.0.0")),
            ("2.0.0", ("2.0.0",)),
        ],
    )
    def test_candidates_walk_down_minors_only(self, version: str, expected: tuple) -> None:
        """Never down a MAJOR boundary: that is where a clip most likely shows dead UI."""
        assert manifest_mod.release_candidates(version) == expected

    def test_an_unparseable_version_has_no_candidates(self) -> None:
        assert manifest_mod.release_candidates("not-a-version") == ()
        assert manifest_mod.running_release("not-a-version") == ""

    def test_release_dir_refuses_an_unsafe_folder_name(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with pytest.raises(ValueError):
                manifest_mod.release_dir("../escape")


# ── fetching ──


def _as_opener(open_fn):
    """Wrap a urlopen-shaped fake as a ``build_opener`` replacement.

    Both clients (manifest, media) call ``build_opener`` so they cannot differ on
    redirect policy, which makes it the one seam a test replaces.
    """
    return lambda *args, **kwargs: SimpleNamespace(open=open_fn)


def _fake_manifest_fetch(by_url: dict, state: SimpleNamespace):
    class _Resp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self, n: int = -1) -> bytes:
            return self._body if n < 0 else self._body[:n]

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def _open(request, timeout=None):  # noqa: ANN001 - OpenerDirector.open
        url = getattr(request, "full_url", str(request))
        state.urls.append(url)
        if url not in by_url:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)  # type: ignore[arg-type]
        return _Resp(by_url[url])

    return _open


class TestManifestFetch:
    @pytest.fixture(autouse=True)
    def _permitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The walk asks the ceiling before every request; these tests are about the walk."""
        monkeypatch.setattr(manifest_mod, "download_denied", lambda: False)

    def test_every_request_of_the_walk_takes_its_own_answer(
        self, signing_key: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A withdrawal between two fallback requests stops the second one.

        The first candidate 404s and the second would verify. With the ceiling
        withdrawn after the first request, the second must not be made: nothing
        rides the permit an earlier request was made on, the same rule the
        download pass applies between a poster and its clip.
        """
        signed = _sign(signing_key, tmp_path, _document(release="0.5.0"))
        state = SimpleNamespace(urls=[])
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(
                _fake_manifest_fetch(
                    {
                        f"{manifest_mod.DEFAULT_CDN_BASE}/0.5.0/manifest.json": json.dumps(
                            signed
                        ).encode()
                    },
                    state,
                )
            ),
        )
        answers = iter([False, True])
        asked: list[bool] = []

        def _denied() -> bool:
            asked.append(True)
            return next(answers)

        monkeypatch.setattr(manifest_mod, "download_denied", _denied)
        manifest, raw = manifest_mod.fetch_manifest("0.6.0")
        assert (manifest, raw) == (None, {})
        assert [u.rsplit("/", 2)[1] for u in state.urls] == ["0.6.0"], "the walk stopped"
        assert len(asked) == 2, "one audited answer per request attempted"

    def test_fetches_verifies_and_stops_at_the_running_release(
        self, signing_key: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signed = _sign(signing_key, tmp_path, _document(release="0.6.0"))
        state = SimpleNamespace(urls=[])
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(
                _fake_manifest_fetch(
                    {
                        f"{manifest_mod.DEFAULT_CDN_BASE}/0.6.0/manifest.json": json.dumps(
                            signed
                        ).encode()
                    },
                    state,
                )
            ),
        )
        manifest, raw = manifest_mod.fetch_manifest("0.6.0")
        assert manifest is not None and manifest.release == "0.6.0"
        assert raw["signature"] == signed["signature"]
        assert len(state.urls) == 1

    def test_falls_back_to_the_newest_lower_release(
        self, signing_key: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A build with no clips of its own still plays the existing library."""
        signed = _sign(signing_key, tmp_path, _document(release="0.4.0"))
        state = SimpleNamespace(urls=[])
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(
                _fake_manifest_fetch(
                    {
                        f"{manifest_mod.DEFAULT_CDN_BASE}/0.4.0/manifest.json": json.dumps(
                            signed
                        ).encode()
                    },
                    state,
                )
            ),
        )
        manifest, _raw = manifest_mod.fetch_manifest("0.6.0")
        assert manifest is not None and manifest.release == "0.4.0"
        # Tried 0.6.0 and 0.5.0 first, in order.
        assert [u.rsplit("/", 2)[1] for u in state.urls] == ["0.6.0", "0.5.0", "0.4.0"]

    def test_a_manifest_disagreeing_with_its_own_location_is_refused(
        self, signing_key: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A misfiled document must not redirect one release's clips into another's."""
        signed = _sign(signing_key, tmp_path, _document(release="0.4.0"))
        state = SimpleNamespace(urls=[])
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(
                _fake_manifest_fetch(
                    {
                        f"{manifest_mod.DEFAULT_CDN_BASE}/0.6.0/manifest.json": json.dumps(
                            signed
                        ).encode()
                    },
                    state,
                )
            ),
        )
        manifest, _raw = manifest_mod.fetch_manifest("0.6.0")
        assert manifest is None

    def test_an_override_manifest_may_declare_any_release_this_build_reads_back(
        self, signing_key: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One mirror document serves a line of releases; it is fetched exactly once."""
        signed = _sign(signing_key, tmp_path, _document(release="0.4.0"))
        state = SimpleNamespace(urls=[])
        override = "https://mirror.example/feature-videos/manifest.json"
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_manifest_fetch({override: json.dumps(signed).encode()}, state)),
        )
        monkeypatch.setenv(manifest_mod.MANIFEST_URL_ENV, override)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest, raw = manifest_mod.fetch_manifest("0.6.0")
            assert manifest is not None and manifest.release == "0.4.0"
            assert state.urls == [override]
            # What was stored is what an offline restart reads back.
            manifest_mod.store_manifest(manifest, raw)
            loaded = manifest_mod.load_cached_manifest("0.6.0")
        assert loaded is not None and loaded.release == "0.4.0"

    def test_an_override_manifest_outside_the_fallback_window_is_refused(
        self, signing_key: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Storing it would cache a document the loader can never find again."""
        signed = _sign(signing_key, tmp_path, _document(release="0.1.0"))
        state = SimpleNamespace(urls=[])
        override = "https://mirror.example/feature-videos/manifest.json"
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_manifest_fetch({override: json.dumps(signed).encode()}, state)),
        )
        monkeypatch.setenv(manifest_mod.MANIFEST_URL_ENV, override)
        manifest, raw = manifest_mod.fetch_manifest("0.6.0")
        assert (manifest, raw) == (None, {})
        assert state.urls == [override], "the same override url is not re-fetched per candidate"

    def test_only_the_operator_url_may_redirect_across_hosts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CDN keeps the host pin; the operator's own env mirror may hop on https."""
        seen: list[bool] = []

        def _record(*_a: object, allow_cross_host_redirects: bool = False, **_k: object):
            seen.append(allow_cross_host_redirects)
            raise urllib.error.URLError("stop here")

        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", _record)
        manifest_mod.fetch_manifest("0.6.0")
        assert seen and not any(seen), "CDN urls are host-pinned"
        seen.clear()
        monkeypatch.setenv(manifest_mod.MANIFEST_URL_ENV, "https://mirror.example/m.json")
        manifest_mod.fetch_manifest("0.6.0")
        assert seen == [True], "one fetch of the operator url, relaxed to https-only"

    def test_an_unsigned_response_yields_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = SimpleNamespace(urls=[])
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(
                _fake_manifest_fetch(
                    {
                        f"{manifest_mod.DEFAULT_CDN_BASE}/{rel}/manifest.json": json.dumps(
                            _document(release=rel)
                        ).encode()
                        for rel in ("0.6.0", "0.5.0", "0.4.0", "0.3.0")
                    },
                    state,
                )
            ),
        )
        manifest, raw = manifest_mod.fetch_manifest("0.6.0")
        assert (manifest, raw) == (None, {})

    def test_an_oversized_response_is_ignored_before_parsing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = SimpleNamespace(urls=[])
        url = f"{manifest_mod.DEFAULT_CDN_BASE}/0.6.0/manifest.json"
        monkeypatch.setattr(
            "kiro_crew.asset_downloader.build_opener",
            _as_opener(_fake_manifest_fetch({url: b"x" * (256 * 1024 + 10)}, state)),
        )
        assert manifest_mod._fetch_json(url) is None

    def test_a_malformed_url_reads_as_no_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A mistyped https override must not take the background task with it.

        urllib raises http.client.InvalidURL before any socket exists. It is not
        an OSError and not a ValueError, so it escapes a tuple that lists only
        those and kills the refresh task that called this.
        """

        def _explode(*_a: object, **_k: object) -> object:
            raise http.client.InvalidURL("nonnumeric port: 'not-a-port'")

        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", _as_opener(_explode))
        assert manifest_mod._fetch_json("https://cdn.example.com:not-a-port/manifest.json") is None


class TestManifestUrlResolution:
    def test_the_default_is_the_cdn_release_folder(self) -> None:
        assert (
            manifest_mod.manifest_url("0.6.0")
            == f"{manifest_mod.DEFAULT_CDN_BASE}/0.6.0/manifest.json"
        )

    def test_the_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(manifest_mod.MANIFEST_URL_ENV, "https://mirror.example/m.json")
        assert manifest_mod.manifest_url("0.6.0") == "https://mirror.example/m.json"
        assert manifest_mod.operator_manifest_url() == "https://mirror.example/m.json"

    def test_there_is_no_config_knob_for_the_manifest_url(self) -> None:
        """A request target the agent's own tools could write is not a preference.

        The override channel is the process environment only: the loader has no
        ``feature_videos_manifest_url`` leaf, and the cache asks the manifest
        module for the url without consulting the dashboard config.
        """
        from kiro_crew.config.sections import DashboardConfig

        assert not hasattr(DashboardConfig(), "feature_videos_manifest_url")
        assert manifest_mod.operator_manifest_url() == ""

    @pytest.mark.parametrize("bad", ["http://mirror.example/m.json", "file:///tmp/m.json", " "])
    def test_a_non_https_override_is_ignored_not_honoured(
        self, bad: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(manifest_mod.MANIFEST_URL_ENV, bad)
        assert manifest_mod.manifest_url("0.6.0") == (
            f"{manifest_mod.DEFAULT_CDN_BASE}/0.6.0/manifest.json"
        )


# ── the on-disk manifest cache ──


class TestManifestOnDisk:
    def test_a_stored_manifest_is_re_verified_on_read(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            signed = _sign(signing_key, tmp_path, _document())
            manifest_mod.store_manifest(_parsed(), signed)
            loaded = manifest_mod.load_cached_manifest("0.6.0")
            assert loaded is not None and loaded.release == "0.6.0"

    def test_an_edited_cache_file_is_refused(self, signing_key: Path, tmp_path: Path) -> None:
        """The cache states cdn_base, so trusting it because we once did is not enough."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            signed = _sign(signing_key, tmp_path, _document())
            manifest_mod.store_manifest(_parsed(), signed)
            path = manifest_mod.cached_manifest_path("0.6.0")
            doctored = json.loads(path.read_text(encoding="utf-8"))
            doctored["cdn_base"] = "https://attacker.example/videos"
            path.write_text(json.dumps(doctored), encoding="utf-8")
            assert manifest_mod.load_cached_manifest("0.6.0") is None

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privilege on Windows")
    def test_a_link_at_the_cached_manifest_name_is_not_followed(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        """A planted link — to a real signed manifest here, to /dev/zero in the finding —
        is refused by the no-follow read rather than read through."""
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text(
            json.dumps(_sign(signing_key, tmp_path, _document())), encoding="utf-8"
        )
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest_mod.ensure_cache_dir("0.6.0")
            manifest_mod.cached_manifest_path("0.6.0").symlink_to(elsewhere)
            assert manifest_mod.load_cached_manifest("0.6.0") is None

    def test_a_cached_manifest_over_the_size_cap_is_not_read_to_the_end(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        """The read stops at the cap the network fetch already enforces; what it has
        by then is not a document, so the file reads as no manifest."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            signed = _sign(signing_key, tmp_path, _document())
            manifest_mod.store_manifest(_parsed(), signed)
            path = manifest_mod.cached_manifest_path("0.6.0")
            body = path.read_text(encoding="utf-8")
            padding = " " * (manifest_mod._MANIFEST_MAX_BYTES + 16)
            path.write_text(padding + body, encoding="utf-8")
            assert manifest_mod.load_cached_manifest("0.6.0") is None

    def test_a_corrupt_cache_file_reads_as_no_manifest(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest_mod.ensure_cache_dir("0.6.0")
            manifest_mod.cached_manifest_path("0.6.0").write_text("{not json", encoding="utf-8")
            assert manifest_mod.load_cached_manifest("0.6.0") is None

    def test_a_lower_release_cache_is_used_for_a_newer_build(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            signed = _sign(signing_key, tmp_path, _document(release="0.4.0"))
            manifest_mod.store_manifest(_parsed(release="0.4.0"), signed)
            loaded = manifest_mod.load_cached_manifest("0.6.0")
            assert loaded is not None and loaded.release == "0.4.0"

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privilege")
    def test_a_symlinked_release_folder_is_refused_and_nothing_lands_outside(
        self, signing_key: Path, tmp_path: Path
    ) -> None:
        """The write path holds the containment the serving route already proves.

        A planted ``<root>/<release>`` link would otherwise make ``atomic_write``
        follow it and put ``manifest.json`` wherever the link points.
        """
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o755)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest_mod.cache_root().mkdir(parents=True)
            (manifest_mod.cache_root() / "0.6.0").symlink_to(outside, target_is_directory=True)
            with pytest.raises(manifest_mod.CacheDirRefused):
                manifest_mod.ensure_cache_dir("0.6.0")
            manifest_mod.store_manifest(_parsed(), _sign(signing_key, tmp_path, _document()))
        assert list(outside.iterdir()) == []
        assert stat.S_IMODE(outside.stat().st_mode) == 0o755  # not tightened through the link
        assert manifest_mod.load_cached_manifest("0.6.0") is None

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privilege")
    def test_a_link_planted_after_the_check_is_not_chmodded_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The race the by-name ``chmod`` lost: the release name passes the link check,
        then becomes a link to a folder outside the cache before the tighten. The
        folder is created and opened relative to the pinned root with no-follow
        semantics, so the planted link is refused, and the owner-only mode is applied
        through the held descriptor — the outside folder's mode is untouched."""
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o755)
        real_refuse = manifest_mod._refuse_link

        def _check_then_plant(path: Path, label: str) -> None:
            real_refuse(path, label)
            if "root" in label:
                planted = manifest_mod.cache_root() / "0.6.0"
                planted.parent.mkdir(parents=True, exist_ok=True)
                if not planted.is_symlink():
                    planted.symlink_to(outside, target_is_directory=True)

        monkeypatch.setattr(manifest_mod, "_refuse_link", _check_then_plant)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with pytest.raises(manifest_mod.CacheDirRefused):
                manifest_mod.ensure_cache_dir("0.6.0")
        assert stat.S_IMODE(outside.stat().st_mode) == 0o755
        assert list(outside.iterdir()) == []

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privilege")
    def test_a_root_swapped_for_a_link_after_the_check_creates_nothing_outside(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The root passes its by-name check, then becomes a link to a folder outside
        the cache. The root is opened no-follow and must prove its real path BEFORE
        the release folder is created under it, so the link is refused and not even
        an empty ``<outside>/<release>`` appears."""
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o755)
        real_refuse = manifest_mod._refuse_link

        def _check_then_swap_root(path: Path, label: str) -> None:
            real_refuse(path, label)
            if "root" in label and not path.is_symlink():
                if path.is_dir():
                    path.rmdir()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(outside, target_is_directory=True)

        monkeypatch.setattr(manifest_mod, "_refuse_link", _check_then_swap_root)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with pytest.raises(manifest_mod.CacheDirRefused):
                manifest_mod.ensure_cache_dir("0.6.0")
        assert list(outside.iterdir()) == []
        assert stat.S_IMODE(outside.stat().st_mode) == 0o755

    def test_an_existing_release_folder_is_tightened_through_its_descriptor(
        self, tmp_path: Path
    ) -> None:
        """A folder created before the guarantee (or loosened since) comes back 0700."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.cache_root() / "0.6.0"
            folder.mkdir(parents=True)
            folder.chmod(0o755)
            assert manifest_mod.ensure_cache_dir("0.6.0") == folder
            if not sys.platform.startswith("win"):
                assert stat.S_IMODE(folder.stat().st_mode) == 0o700

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privilege")
    def test_a_junction_at_the_release_folder_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Windows junction lstat's as a plain directory; it is refused by its reparse
        tag through ``is_link_or_junction``. Simulated here, since Linux has none."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.ensure_cache_dir("0.6.0")
            monkeypatch.setattr(
                manifest_mod.platform_compat,
                "is_link_or_junction",
                lambda path: Path(path) == folder,
            )
            with pytest.raises(manifest_mod.CacheDirRefused, match="symlink"):
                manifest_mod.ensure_cache_dir("0.6.0")

    def test_a_symlinked_cache_root_is_refused(self, tmp_path: Path) -> None:
        """A planted root moves every release folder at once, so it is checked too."""
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest_mod.cache_root().parent.mkdir(parents=True, exist_ok=True)
            manifest_mod.cache_root().symlink_to(outside, target_is_directory=True)
            with pytest.raises(manifest_mod.CacheDirRefused):
                manifest_mod.ensure_cache_dir("0.6.0")
        assert list(outside.iterdir()) == []

    def test_a_file_where_the_release_folder_should_be_is_refused(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest_mod.cache_root().mkdir(parents=True)
            (manifest_mod.cache_root() / "0.6.0").write_text("not a dir", encoding="utf-8")
            with pytest.raises(manifest_mod.CacheDirRefused):
                manifest_mod.ensure_cache_dir("0.6.0")

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privilege")
    def test_the_clip_transfer_refuses_a_symlinked_release_folder(self, tmp_path: Path) -> None:
        """Both transfers ask ensure_cache_dir for the folder, so the gate covers them."""
        outside = tmp_path / "outside"
        outside.mkdir()
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            manifest_mod.cache_root().mkdir(parents=True)
            (manifest_mod.cache_root() / "0.6.0").symlink_to(outside, target_is_directory=True)
            with patch.object(
                cache_mod.asset_downloader, "download_to", side_effect=AssertionError("no transfer")
            ):
                ok, err = cache_mod.FeatureVideoCache()._download_entry(
                    manifest.entries[0], manifest, 0
                )
        assert ok is False and "cache directory unavailable" in err
        assert list(outside.iterdir()) == []

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX mode bits")
    def test_the_cache_is_owner_only(self, signing_key: Path, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest_mod.store_manifest(_parsed(), _sign(signing_key, tmp_path, _document()))
            folder = manifest_mod.release_dir("0.6.0")
            assert stat.S_IMODE(folder.stat().st_mode) == 0o700
            path = manifest_mod.cached_manifest_path("0.6.0")
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ── eviction ──


class TestPinnedReleaseDir:
    """The write side holds the folder open and proves, on the descriptor, where it is."""

    def test_the_handle_is_the_checked_folder(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with manifest_mod.pinned_release_dir("0.6.0") as folder:
                assert folder.directory == manifest_mod.release_dir("0.6.0")
                folder.write_text("note.txt", "x")
            assert (manifest_mod.release_dir("0.6.0") / "note.txt").read_text() == "x"

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privilege on Windows")
    def test_a_root_swapped_after_the_check_is_refused_by_the_descriptors_real_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The race the pin alone cannot see: the swap lands BEFORE the open resolves.

        ``ensure_cache_dir`` and ``checked_cache_root`` pass, then the whole cache
        root is renamed away and a link to an outside tree — holding a real
        ``0.6.0`` folder — takes its name. The pinned open resolves the parent by
        name, follows that link and pins the outside folder perfectly. What refuses
        it is the kernel's own path for the open descriptor, which is not
        ``<canonical root>/0.6.0``.
        """
        outside = tmp_path / "outside"
        (outside / "0.6.0").mkdir(parents=True)
        real_pin = asset_downloader.pin_target_dir

        def _swap_then_pin(path: Path, **kwargs: object):
            root = manifest_mod.cache_root()
            root.rename(tmp_path / "moved-root")
            root.symlink_to(outside, target_is_directory=True)
            return real_pin(path, **kwargs)

        monkeypatch.setattr(asset_downloader, "pin_target_dir", _swap_then_pin)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with pytest.raises(manifest_mod.CacheDirRefused, match="not where its name says"):
                with manifest_mod.pinned_release_dir("0.6.0"):
                    pass
        assert list((outside / "0.6.0").iterdir()) == []

    def test_a_platform_that_cannot_read_the_real_path_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed: no witness, no write."""
        monkeypatch.setattr(manifest_mod.pinned_fs, "fd_real_path", lambda _fd: None)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            with pytest.raises(manifest_mod.CacheDirRefused, match="cannot be located"):
                with manifest_mod.pinned_release_dir("0.6.0"):
                    pass

    def test_a_refused_folder_fails_the_entry_with_a_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The download pass reports the refusal; it does not crash or write."""
        monkeypatch.setattr(manifest_mod.pinned_fs, "fd_real_path", lambda _fd: None)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest = _parsed()
            cache = cache_mod.FeatureVideoCache()
            ok, err = cache._download_entry(manifest.entries[0], manifest, 0)
            assert ok is False
            assert err.startswith("cache directory unavailable:")
            assert not (manifest_mod.release_dir("0.6.0") / "hosted-clip.jpg").exists()

    def test_a_receipt_from_a_path_goes_through_the_same_pin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.ensure_cache_dir("0.6.0")
            monkeypatch.setattr(manifest_mod.pinned_fs, "fd_real_path", lambda _fd: None)
            assert cache_mod.record_verified(folder, "hosted-clip.mp4", _CLIP_SHA) is False
            assert cache_mod.recorded_sha256(folder, "hosted-clip.mp4") == ""


class TestEviction:
    def _seed_releases(self, sizes: "dict[str, int]") -> None:
        for i, (release, size) in enumerate(sizes.items()):
            folder = manifest_mod.ensure_cache_dir(release)
            (folder / "clip.mp4").write_bytes(b"x" * size)
            # Distinct mtimes, oldest first in insertion order.
            stamp = time.time() - (len(sizes) - i) * 100
            os.utime(folder, (stamp, stamp))

    def test_evicts_oldest_first_until_the_cap_is_met(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            self._seed_releases({"0.3.0": 400, "0.4.0": 400, "0.5.0": 400, "0.6.0": 400})
            evicted = cache_mod.evict("0.6.0", max_bytes=900)
            assert evicted == ["0.3.0", "0.4.0"]
            remaining = {r for r, _p, _m, _s in cache_mod.release_folders()}
            assert remaining == {"0.5.0", "0.6.0"}

    def test_the_running_release_is_never_evicted_even_over_the_cap(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            self._seed_releases({"0.6.0": 5000})
            assert cache_mod.evict("0.6.0", max_bytes=10) == []
            assert manifest_mod.release_dir("0.6.0").is_dir()

    @pytest.mark.parametrize(
        ("max_mb", "expected"),
        [
            (500.0, 500 * 1024 * 1024),
            (0.0, 0),
            (-5.0, 0),
            (float("nan"), 0),
            (float("inf"), 0),
            (1e308, cache_mod.MAX_CACHE_BYTES),
            (1e300, cache_mod.MAX_CACHE_BYTES),
        ],
    )
    def test_the_ceiling_is_always_a_byte_count(self, max_mb: float, expected: int) -> None:
        """A configured megabyte value becomes an int, or eviction cannot run at all.

        1e308 is the one that bites: the multiply alone overflows to inf, and
        int(inf) raises OverflowError out of the eviction pass.
        """
        assert cache_mod.cache_ceiling_bytes(max_mb) == expected

    def test_an_absurd_configured_ceiling_evicts_nothing_instead_of_raising(
        self, tmp_path: Path
    ) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            self._seed_releases({"0.5.0": 400, "0.6.0": 400})
            cache = cache_mod.FeatureVideoCache()
            dashboard = SimpleNamespace(feature_videos_cache_max_mb=1e308)
            cache._evict_now("0.6.0", dashboard)
            remaining = {r for r, _p, _m, _s in cache_mod.release_folders()}
            assert remaining == {"0.5.0", "0.6.0"}

    def test_no_cap_evicts_nothing(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            self._seed_releases({"0.4.0": 10, "0.6.0": 10})
            assert cache_mod.evict("0.6.0", max_bytes=0) == []

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privilege")
    def test_a_symlinked_cache_root_is_never_scanned_or_evicted(self, tmp_path: Path) -> None:
        """rmtree is irreversible: a root that is a link lists nothing and deletes nothing."""
        outside = tmp_path / "elsewhere"
        (outside / "0.3.0").mkdir(parents=True)
        (outside / "0.3.0" / "clip.mp4").write_bytes(b"x" * 400)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest_mod.cache_root().parent.mkdir(parents=True, exist_ok=True)
            manifest_mod.cache_root().symlink_to(outside, target_is_directory=True)
            assert cache_mod.release_folders() == []
            assert cache_mod.evict("0.6.0", max_bytes=1) == []
            # And the deletion primitive itself refuses even when handed the path.
            assert cache_mod._remove_release("0.3.0", manifest_mod.cache_root() / "0.3.0") is False
        assert (outside / "0.3.0" / "clip.mp4").is_file()

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privilege")
    def test_a_symlinked_release_folder_is_not_an_eviction_candidate(self, tmp_path: Path) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "keep.txt").write_text("x", encoding="utf-8")
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            self._seed_releases({"0.6.0": 10})
            (manifest_mod.cache_root() / "0.3.0").symlink_to(outside, target_is_directory=True)
            assert {r for r, _p, _m, _s in cache_mod.release_folders()} == {"0.6.0"}
            assert cache_mod._remove_release("0.3.0", manifest_mod.cache_root() / "0.3.0") is False
        assert (outside / "keep.txt").is_file()

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privilege")
    def test_a_release_folder_swapped_after_the_listing_is_not_deleted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check-to-delete window: the folder the list saw is renamed away and a
        link to the user's data takes its name before the removal. The pinned open
        refuses the link; nothing outside the cache is touched."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"mine")
        real_remove = pinned_fs.remove_tree_pinned

        def _swap_then_remove(resolved_path: str, **kwargs: object):
            folder = Path(resolved_path)
            folder.rename(tmp_path / "moved")
            folder.symlink_to(outside, target_is_directory=True)
            return real_remove(resolved_path, **kwargs)

        monkeypatch.setattr(pinned_fs, "remove_tree_pinned", _swap_then_remove)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.3.0")
            assert cache_mod._remove_release("0.3.0", manifest_mod.release_dir("0.3.0")) is False
        assert (outside / "keep.txt").read_bytes() == b"mine"
        assert (tmp_path / "moved" / "hosted-clip.mp4").exists()

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privilege")
    def test_a_root_swapped_after_the_check_is_refused_by_the_descriptors_real_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The swap the pin alone cannot see: the ROOT becomes a link to an outside tree
        holding a real ``0.3.0`` folder, so the pinned walk opens that folder perfectly.
        The real path of the open descriptor is what refuses the delete."""
        outside = tmp_path / "outside"
        (outside / "0.3.0").mkdir(parents=True)
        (outside / "0.3.0" / "keep.txt").write_bytes(b"mine")
        real_remove = pinned_fs.remove_tree_pinned

        def _swap_root_then_remove(resolved_path: str, **kwargs: object):
            cache_root = manifest_mod.cache_root()
            cache_root.rename(tmp_path / "moved-root")
            cache_root.symlink_to(outside, target_is_directory=True)
            return real_remove(resolved_path, **kwargs)

        monkeypatch.setattr(pinned_fs, "remove_tree_pinned", _swap_root_then_remove)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            _write_media("0.3.0")
            assert cache_mod._remove_release("0.3.0", manifest_mod.release_dir("0.3.0")) is False
        assert (outside / "0.3.0" / "keep.txt").read_bytes() == b"mine"

    def test_the_held_handle_fallback_removes_a_real_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The branch a platform without descriptor-relative walks takes (Windows),
        exercised here on purpose."""
        monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = _write_media("0.3.0")
            assert cache_mod._remove_release("0.3.0", folder) is True
            assert not folder.exists()

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privilege")
    def test_the_held_handle_fallback_refuses_a_link_at_the_folder_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"mine")
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            manifest_mod.cache_root().mkdir(parents=True)
            (manifest_mod.cache_root() / "0.3.0").symlink_to(outside, target_is_directory=True)
            assert cache_mod._remove_release("0.3.0", manifest_mod.cache_root() / "0.3.0") is False
        assert (outside / "keep.txt").read_bytes() == b"mine"

    def test_an_unrecognized_directory_is_left_alone(self, tmp_path: Path) -> None:
        """We never created it, so we never delete it."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            stray = manifest_mod.cache_root() / "not-a-release"
            stray.mkdir(parents=True)
            (stray / "keep.txt").write_text("x", encoding="utf-8")
            cache_mod.evict("0.6.0", max_bytes=1)
            assert stray.is_dir()


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
        assert body["download_state"] == cache_mod.STATE_IDLE

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
