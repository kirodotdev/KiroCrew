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

This file holds the manifest half: verification, parsing, release resolution, fetching,
the on-disk manifest cache and eviction. The rest of the suite sits beside it:
``test_feature_videos_hosted_download_pass.py`` (cached-ness and the download pass),
``test_feature_videos_hosted_serving.py`` (the media route and source selection) and
``test_feature_videos_hosted_routes.py`` (next, status, fetch-all and the governance
probe). Shared fixtures and builders are in ``feature_videos_hosted_helpers.py``.
"""

from __future__ import annotations

import base64
import errno
import http.client
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import feature_video_fixture as fixture
import pytest
from feature_videos_hosted_helpers import _fresh_cache  # noqa: F401 - fixture
from feature_videos_hosted_helpers import _no_network  # noqa: F401 - fixture
from feature_videos_hosted_helpers import (
    _CDN,
    _CLIP_SHA,
    _as_opener,
    _document,
    _entry,
    _parsed,
    _write_media,
)

from kiro_crew import asset_downloader
from kiro_crew import feature_videos_cache as cache_mod
from kiro_crew import feature_videos_manifest as manifest_mod
from kiro_crew import pinned_fs
from kiro_crew.platform import feed_trust

# ── fixtures ──


@pytest.fixture(scope="session")
def _throwaway_pair(tmp_path_factory: pytest.TempPathFactory) -> "tuple[Path, Path, str]":
    """One throwaway RSA pair for the whole session, with its key id.

    Session-scoped because a 3072-bit keygen is ~0.5s and a dozen tests want a
    signing key: minting per test spent most of this file's runtime on openssl.
    The key id is computed here too, once: ``pin_fixture_key`` recomputes it with
    an ``openssl pkey`` spawn on every call, and the pair does not change between
    tests. The PINNING stays per-test (below), so no test inherits another's patch.
    """
    private, public = fixture.mint_throwaway_key(tmp_path_factory.mktemp("fv-signing-key"))
    return private, public, fixture.key_id_of(public)


@pytest.fixture(scope="module")
def _fixture_key_id() -> str:
    """The committed fixture key's id, computed once per module (one openssl spawn)."""
    return fixture.key_id_of(fixture.PUBLIC_KEY_PATH)


def _pin(monkeypatch: pytest.MonkeyPatch, public: Path, key_id: str) -> str:
    """``fixture.pin_fixture_key`` with the key id already known.

    The same three pins, in the same order, minus the per-call ``openssl pkey``
    spawn that derives the id — the callers above hold a cached one. The openssl
    pin stays: production resolves it from fixed system directories, and a host
    whose openssl lives elsewhere would otherwise fail every positive case for a
    reason unrelated to what it tests.
    """
    monkeypatch.setattr(feed_trust, "trusted_system_bin", lambda _n: fixture.openssl_or_skip())
    monkeypatch.setattr(
        feed_trust,
        "PINNED_PUBLIC_KEY_B64",
        base64.b64encode(public.read_bytes()).decode("ascii"),
    )
    monkeypatch.setattr(feed_trust, "PINNED_KEY_ID", key_id)
    return key_id


@pytest.fixture()
def signing_key(_throwaway_pair: "tuple[Path, Path, str]", monkeypatch: pytest.MonkeyPatch) -> Path:
    """The session key, with ``feed_trust``'s pins repointed at it for this test.

    Both halves come from ``feature_video_fixture``, which is also what the
    publishing tool's tests use — one helper, so a key minted here and a key minted
    there cannot differ in a way that hides an encoding disagreement.
    """
    private, public, key_id = _throwaway_pair
    _pin(monkeypatch, public, key_id)
    return private


def _sign(private: Path, tmp_path: Path, document: dict) -> dict:
    """Attach a real signature over the canonical payload."""
    return fixture.sign_document(private, document, tmp_path)


class TestSharedFixture:
    """The committed cross-tool fixture: ``test/fixtures/feature-videos/``.

    The publishing tool's tests verify these same bytes, so what is pinned here is
    the signed byte FORMAT rather than any authority. The private half is minted on
    demand instead of committed — see the header of
    ``test/fixtures/feature-videos/regenerate.py``.
    """

    @pytest.fixture()
    def pinned(self, monkeypatch: pytest.MonkeyPatch, _fixture_key_id: str) -> str:
        return _pin(monkeypatch, fixture.PUBLIC_KEY_PATH, _fixture_key_id)

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
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout
        mis_signed = {**document, "signature": base64.b64encode(signature).decode("ascii")}
        assert manifest_mod.verify_manifest(mis_signed) is False

    def test_the_fixture_key_is_not_the_production_trust_root(self, _fixture_key_id: str) -> None:
        """A test key must never be able to become the thing that grants trust.

        Asserted WITHOUT the pinning fixture, against the real committed pins: if
        someone ever pasted this key into ``feed_trust``, every fixture-signed
        document would verify on a shipped build.
        """
        assert _fixture_key_id != feed_trust.PINNED_KEY_ID
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

    @pytest.mark.skipif(
        not pinned_fs.supports_pinned_walk(), reason="the descriptor-relative create path"
    )
    def test_a_release_folder_removed_before_its_open_names_the_whole_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Creating the folder and opening it are two calls, and a removal fits between.

        The `FileExistsError` half of that race was tolerated and this half was not
        (GH-12043): the open failed with `ENOENT` on the bare relative name `'0.6.0'`,
        which names no directory and reads as a working-directory bug. It is reported
        with the whole path now -- and still reported, not re-created, because the
        actor that removes a release folder here is the cache's own eviction.
        """
        real_mkdir = os.mkdir
        removals: list[int] = []

        def vanishing(name: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            real_mkdir(name, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
            if name == "0.6.0":
                removals.append(1)
                os.rmdir(name, dir_fd=dir_fd)  # type: ignore[arg-type]

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.release_dir("0.6.0")
            monkeypatch.setattr(os, "mkdir", vanishing)
            with pytest.raises(FileNotFoundError) as excinfo:
                manifest_mod.ensure_cache_dir("0.6.0")
        assert removals == [1], "the race did not happen, so this asserts nothing"
        assert excinfo.value.filename == str(folder)
        assert "release folder was removed" in str(excinfo.value)
        assert not folder.exists(), "an evicted release folder was re-created"

    @pytest.mark.skipif(
        not pinned_fs.supports_pinned_walk(), reason="the descriptor-relative create path"
    )
    def test_a_cache_root_removed_under_its_pin_names_the_release_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other end of the same sequence: the root goes, so the `mkdir` has nowhere.

        Reported separately from the folder's own removal because the two are
        different conditions, and reported with the path for the same reason: the
        errno carries only `'0.6.0'`.
        """
        real_mkdir = os.mkdir

        def removing_the_root(
            name: object, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> None:
            if name == "0.6.0":
                os.rmdir(manifest_mod.cache_root())
            real_mkdir(name, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.release_dir("0.6.0")
            monkeypatch.setattr(os, "mkdir", removing_the_root)
            with pytest.raises(FileNotFoundError) as excinfo:
                manifest_mod.ensure_cache_dir("0.6.0")
        assert excinfo.value.filename == str(folder)
        assert "cache root was removed" in str(excinfo.value)

    def test_the_by_name_branch_also_names_the_whole_path_when_the_folder_vanishes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The platform WITHOUT `dir_fd` reports the removal with the path too.

        Review asked for the claim to be measured rather than asserted in prose, and
        measuring it found the prose wrong. That branch pins by path, and on the
        platform it exists for it does so through `CreateFileW`: the `ctypes.WinError`
        raised when the folder is gone carries NO `filename` and no path in its
        message, so the operator was told only that the system cannot find the file.
        A POSIX `os.open` on the same code path DOES set `filename`, which is why
        running the branch on this host proves nothing on its own -- the stub below
        reproduces the one thing that differs, an `ENOENT` with nothing attached.
        """
        monkeypatch.setattr(manifest_mod.pinned_fs, "supports_pinned_walk", lambda: False)
        real_mkdir = os.mkdir
        real_pin = manifest_mod.platform_compat.pin_directory
        pinned_by_name: list[str] = []

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.release_dir("0.6.0")

            def windows_shaped_pin(path: object) -> int:
                pinned_by_name.append(str(path))
                if Path(str(path)) == folder:
                    # What `ctypes.WinError(ERROR_PATH_NOT_FOUND)` produces: an errno
                    # and a message, and no filename whatsoever.
                    raise FileNotFoundError(2, "The system cannot find the file specified")
                return real_pin(path)  # type: ignore[arg-type]

            def vanishing(name: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
                real_mkdir(name, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
                if Path(str(name)) == folder:
                    os.rmdir(name)  # type: ignore[arg-type]

            monkeypatch.setattr(manifest_mod.platform_compat, "pin_directory", windows_shaped_pin)
            monkeypatch.setattr(os, "mkdir", vanishing)
            with pytest.raises(FileNotFoundError) as excinfo:
                manifest_mod.ensure_cache_dir("0.6.0")

        assert str(folder) in pinned_by_name, "the by-name branch was not the one exercised"
        assert excinfo.value.filename == str(folder)
        assert "removed between its creation and its open" in str(excinfo.value)
        assert not folder.exists()

    def test_the_by_name_branch_translates_a_not_found_that_is_not_the_subclass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The translation keys on the CONDITION, not on the exception class.

        Review asked whether `pin_directory`'s vanished-folder error really arrives
        as `FileNotFoundError` on a real Windows host, and observed that if it does
        not, the translation never fires. Rather than depend on CPython's
        winerror-to-subclass mapping, the handler tests errno and `winerror`. This
        raises a PLAIN `OSError` carrying `ENOENT` -- the shape the doubt describes --
        and asserts the path still arrives.
        """
        monkeypatch.setattr(manifest_mod.pinned_fs, "supports_pinned_walk", lambda: False)
        real_mkdir = os.mkdir
        real_pin = manifest_mod.platform_compat.pin_directory

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.release_dir("0.6.0")

            def bare_oserror_pin(path: object) -> int:
                # The branch pins the cache ROOT first; only the release folder is
                # made to fail.
                if Path(str(path)) != folder:
                    return real_pin(path)  # type: ignore[arg-type]
                # Constructing OSError(ENOENT, ...) would be mapped to
                # FileNotFoundError by CPython, which is the very thing not to rely
                # on, so the errno is attached after construction.
                exc = OSError()
                exc.errno = errno.ENOENT
                raise exc

            def vanishing(name: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
                real_mkdir(name, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
                if Path(str(name)) == folder:
                    os.rmdir(name)  # type: ignore[arg-type]

            monkeypatch.setattr(manifest_mod.platform_compat, "pin_directory", bare_oserror_pin)
            monkeypatch.setattr(os, "mkdir", vanishing)
            with pytest.raises(FileNotFoundError) as excinfo:
                manifest_mod.ensure_cache_dir("0.6.0")

        assert excinfo.value.filename == str(folder)
        assert "removed between its creation and its open" in str(excinfo.value)

    def test_the_by_name_branch_does_not_swallow_an_unrelated_pin_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Widening the handler to `OSError` must not absorb errors it never owned.

        The handler catches `OSError` so a not-found reaches the translation whatever
        class the platform picked; this pins the other half of that widening, which is
        that anything NOT a not-found still propagates as itself.
        """
        monkeypatch.setattr(manifest_mod.pinned_fs, "supports_pinned_walk", lambda: False)
        real_pin = manifest_mod.platform_compat.pin_directory

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            folder = manifest_mod.release_dir("0.6.0")

            def refusing_pin(path: object) -> int:
                if Path(str(path)) != folder:
                    return real_pin(path)  # type: ignore[arg-type]
                raise PermissionError(errno.EACCES, "permission denied")

            monkeypatch.setattr(manifest_mod.platform_compat, "pin_directory", refusing_pin)
            with pytest.raises(PermissionError) as excinfo:
                manifest_mod.ensure_cache_dir("0.6.0")

        assert "removed between its creation and its open" not in str(excinfo.value)


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
