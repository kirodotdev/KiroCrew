"""The digest-verified ffmpeg decoder store (``kiro_crew.stt.decoder``).

Every test here is offline: the network fetch is replaced at
``decoder.stream_pinned_payload``, the ONE seam both the whisper weights and the
decoder go through, and every write lands under ``tmp_path`` because
``store_dir`` (or the CLI's target directory) is pinned per test. Nothing spawns
a process except the resolver test, which authenticates a tiny shell script
standing in for the real executable -- the pin is monkeypatched to that
script's own digest, so the check under test is the real one.

The suite's conftest sets ``KIROCREW_SKIP_MODEL_DOWNLOAD=1`` for every test, so a
test that exercises the fetch has to unset it deliberately; that switch has its
own test below.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import threading
from pathlib import Path

import pytest

from kiro_crew import platform_compat as _pc
from kiro_crew import transcribe
from kiro_crew.stt import decoder
from kiro_crew.stt import models as stt_models

# ── helpers ──────────────────────────────────────────────────────────────

_PAYLOAD = b"#!/bin/sh\nexit 0\n"


def _artifact(
    payload: bytes = _PAYLOAD, *, filename: str = "ffmpeg-test"
) -> decoder.DecoderArtifact:
    """A pinned artifact whose size and digest describe *payload*."""
    return decoder.DecoderArtifact(
        platform_key="test-arch",
        filename=filename,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _pin(monkeypatch, tmp_path: Path, artifact: decoder.DecoderArtifact) -> None:
    """Make *artifact* this host's decoder and *tmp_path* the store."""
    monkeypatch.setattr(decoder, "artifact_for", lambda *a, **k: artifact)
    monkeypatch.setattr(decoder, "store_dir", lambda: tmp_path)


def _serve(monkeypatch, body: bytes, calls: list[dict] | None = None) -> None:
    """Answer the store's one network call with *body*, offline.

    Patched at the shared streamer rather than at ``urlopen`` so the test states
    which seam it stands in for; the streamer's own pin enforcement has its own
    coverage in ``test_stt_engine_download.py``. This stand-in keeps the part of
    that contract the store relies on -- bytes that miss the pin raise
    ``ModelDownloadError`` -- but writes the body FIRST, so a refusal leaves a
    partial staging file behind for the caller to clean up, the way a real
    mid-stream digest failure does. Substituted on ``decoder``'s OWN global, which
    is the name the fetch reads.
    """

    def _stream(url, *, label, expected_size, expected_sha256, write, on_progress=None, **kw):
        if calls is not None:
            calls.append(
                {
                    "url": url,
                    "label": label,
                    "expected_size": expected_size,
                    "expected_sha256": expected_sha256,
                }
            )
        write(body)
        digest = hashlib.sha256(body).hexdigest()
        if len(body) != expected_size or digest != expected_sha256:
            raise stt_models.ModelDownloadError(
                f"{label}: sha256 mismatch (got {digest[:16]}…, expected {expected_sha256[:16]}…)"
            )
        if on_progress is not None:
            on_progress(len(body), expected_size)

    monkeypatch.setattr(decoder, "stream_pinned_payload", _stream)


def _refuse_network(monkeypatch, why: str) -> None:
    def _unexpected(*a, **k):
        raise AssertionError(why)

    monkeypatch.setattr(decoder, "stream_pinned_payload", _unexpected)


def _allow_download(monkeypatch) -> None:
    monkeypatch.delenv(stt_models.SKIP_DOWNLOAD_ENV, raising=False)


def _staging_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.part"))


# ── platform selection ───────────────────────────────────────────────────


class TestPlatformSelection:
    """Which pinned artifact a host resolves to, and which hosts resolve to none.

    Parametrised over the reported ``system``/``machine`` because the SAME
    hardware reports different strings per OS — Windows says ``AMD64`` and macOS
    ``arm64`` where Linux says ``x86_64`` and ``aarch64`` — so matching on the raw
    value would leave the Windows and macOS pins unreachable on their own hosts.
    """

    @pytest.mark.parametrize(
        ("system", "machine", "expected"),
        [
            ("Linux", "x86_64", "ffmpeg-linux-x86_64"),
            ("Linux", "AMD64", "ffmpeg-linux-x86_64"),
            ("Linux", "aarch64", "ffmpeg-linux-aarch64"),
            ("Linux", "arm64", "ffmpeg-linux-aarch64"),
            ("Darwin", "arm64", "ffmpeg-macos-aarch64"),
            ("Windows", "AMD64", "ffmpeg-windows-x86_64.exe"),
            ("Windows", "x86_64", "ffmpeg-windows-x86_64.exe"),
        ],
    )
    def test_a_shipped_platform_resolves_its_pinned_artifact(self, system, machine, expected):
        artifact = decoder.artifact_for(system, machine)
        assert artifact is not None
        assert artifact.filename == expected
        assert artifact.platform_key == decoder.platform_key(system, machine)

    @pytest.mark.parametrize(
        ("system", "machine"),
        [
            ("Linux", "armv7l"),  # 32-bit ARM: no pinned build
            ("Linux", "s390x"),
            ("Windows", "ARM64"),  # no windows-aarch64 build
            ("Windows", "i686"),  # win32 is deliberately not shipped
            ("Darwin", "x86_64"),  # the Intel backend ships no decoder
            ("FreeBSD", "x86_64"),
            ("Darwin", "ppc"),
        ],
    )
    def test_a_platform_with_no_pin_is_unsupported_rather_than_guessed(self, system, machine):
        """A composed-but-absent key would read as a typo; ``None`` is the answer.

        The endpoint turns this into ``auto_fetch: unsupported``, which is what
        makes the settings page offer the manual route instead of a retry.
        """
        assert decoder.artifact_for(system, machine) is None

    def test_an_unmapped_system_or_isa_has_no_key(self):
        assert decoder.platform_key("FreeBSD", "x86_64") == ""
        assert decoder.platform_key("Linux", "ppc64le") == ""

    def test_the_intel_mac_executable_is_never_pinned(self):
        """No table may offer the release's macOS Intel executable.

        The Intel backend ships no decoder. A pin for it would let the store fetch
        it onto a host and, through the shared table, re-admit it as a desktop
        payload.
        """
        intel = "ffmpeg-macos-x86_64"
        assert decoder.DECODERLESS_BUNDLE_PLATFORM == "macos-x86_64"
        assert "macos-x86_64" not in {a.platform_key for a in decoder.ARTIFACTS}
        assert intel not in decoder.PACKAGED_FFMPEG_ARTIFACTS
        assert "macos-x86_64" not in transcribe._SHIPPED_FFMPEG_PLATFORMS
        assert intel not in transcribe._SIGNER_REWRITTEN_FFMPEG_ARTIFACTS

    def test_each_artifact_is_fetched_from_the_pinned_release(self):
        for artifact in decoder.ARTIFACTS:
            assert artifact.url == (
                "https://github.com/kirodotdev/KiroCrew/releases/download/"
                f"{decoder.DECODER_RELEASE}/{artifact.filename}"
            )


class TestBundleCarriesDecoder:
    """The one answer to "does this desktop release own its decoder"."""

    @pytest.mark.parametrize(
        ("bundled", "system", "machine", "expected"),
        [
            (True, "Darwin", "arm64", True),
            (True, "Linux", "x86_64", True),
            (True, "Windows", "AMD64", True),
            # The x64 Windows release on an ARM64 PC: Python reports the physical
            # CPU there, and no pin exists for it, yet the release carries its
            # x64 decoder and must keep using it.
            (True, "Windows", "ARM64", True),
            # The Intel half of the universal app ships no decoder, so it must not
            # be told its payload is damaged or be kept from a system FFmpeg.
            (True, "Darwin", "x86_64", False),
            # A source install never owns a packaged decoder, pinned platform or not.
            (False, "Darwin", "arm64", False),
            (False, "Darwin", "x86_64", False),
        ],
    )
    def test_true_for_every_release_but_the_intel_mac_backend(
        self, monkeypatch, bundled, system, machine, expected
    ):
        real_platform_key = decoder.platform_key
        monkeypatch.setattr(decoder.platform_compat, "is_bundled_interpreter", lambda: bundled)
        monkeypatch.setattr(
            decoder, "platform_key", lambda *_a, **_k: real_platform_key(system, machine)
        )
        assert decoder.bundle_carries_decoder() is expected

    def test_every_pinned_artifact_is_a_shipped_desktop_platform(self):
        """The store may not offer a platform the desktop matrix does not ship.

        ``transcribe._SHIPPED_FFMPEG_PLATFORMS`` is the maintainer-owned set, so
        tying to it means one edit (adding a build leg) cannot leave the store
        fetching bytes nothing else authenticates.
        """
        assert {a.platform_key for a in decoder.ARTIFACTS} == (transcribe._SHIPPED_FFMPEG_PLATFORMS)

    def test_the_artifact_table_is_the_one_the_resolver_authenticates_against(self):
        """Two copies would fail as a download that then refuses to execute."""
        assert transcribe._PACKAGED_FFMPEG_ARTIFACTS is decoder.PACKAGED_FFMPEG_ARTIFACTS
        for artifact in decoder.ARTIFACTS:
            assert decoder.PACKAGED_FFMPEG_ARTIFACTS[artifact.filename] == (
                artifact.size_bytes,
                artifact.sha256,
            )


# ── where the store lives ────────────────────────────────────────────────


class TestStoreLayout:
    def test_the_store_lives_under_the_write_protected_models_tree(self, tmp_path, monkeypatch):
        """A sibling of the whisper weights, so both share one fenced directory."""
        monkeypatch.setattr(decoder, "models_dir", lambda: tmp_path / "models" / "whisper")
        assert decoder.store_dir() == tmp_path / "models" / "ffmpeg"

    def test_installed_path_is_the_pinned_filename_in_the_store(self, tmp_path, monkeypatch):
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        assert decoder.installed_path(artifact) == tmp_path / artifact.filename
        assert decoder.installed_path() == tmp_path / artifact.filename

    def test_an_unsupported_platform_has_no_installed_path(self, tmp_path, monkeypatch):
        _pin(monkeypatch, tmp_path, None)  # type: ignore[arg-type]
        assert decoder.installed_path() is None

    def test_is_present_compares_the_pinned_size_only(self, tmp_path, monkeypatch):
        """A UI-poll answer, not a trust check: same-size wrong bytes count."""
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        target = tmp_path / artifact.filename

        assert decoder.is_present(artifact) is False
        target.write_bytes(_PAYLOAD[:-1])
        assert decoder.is_present(artifact) is False
        target.write_bytes(b"#!/bin/sh\nexit 9\n")  # same length, wrong content
        assert decoder.is_present(artifact) is True


# ── installing one artifact ──────────────────────────────────────────────


class TestDownloadInto:
    """``_download_into``: the one install path the store and the desktop build share."""

    def test_a_verified_payload_is_installed_as_an_executable(self, monkeypatch, tmp_path):
        artifact = _artifact()
        calls: list[dict] = []
        _serve(monkeypatch, _PAYLOAD, calls)
        target_dir = tmp_path / "not-yet" / "bin"

        path = decoder._download_into(artifact, target_dir)

        assert path == target_dir / artifact.filename
        assert path.read_bytes() == _PAYLOAD
        if _pc.IS_POSIX:
            assert stat.S_IMODE(path.stat().st_mode) == 0o755
        assert calls == [
            {
                "url": artifact.url,
                "label": artifact.filename,
                "expected_size": artifact.size_bytes,
                "expected_sha256": artifact.sha256,
            }
        ]
        # A leftover `.part` is a partial payload in a directory the resolver
        # enumerates.
        assert _staging_files(target_dir) == []

    def test_progress_is_forwarded_to_the_caller(self, monkeypatch, tmp_path):
        artifact = _artifact()
        _serve(monkeypatch, _PAYLOAD)
        seen: list[tuple[int, int]] = []

        decoder._download_into(artifact, tmp_path, lambda done, total: seen.append((done, total)))

        assert seen == [(artifact.size_bytes, artifact.size_bytes)]

    def test_a_refused_payload_leaves_no_file_at_all(self, monkeypatch, tmp_path):
        artifact = _artifact()
        _serve(monkeypatch, b"#!/bin/sh\nexit 9\n")

        with pytest.raises(stt_models.ModelDownloadError, match="sha256 mismatch"):
            decoder._download_into(artifact, tmp_path)

        assert list(tmp_path.iterdir()) == []

    def test_a_refused_payload_keeps_the_existing_target(self, monkeypatch, tmp_path):
        """The rename is the only write to the target, so a failure cannot touch it."""
        artifact = _artifact()
        target = tmp_path / artifact.filename
        target.write_bytes(b"previous decoder")
        _serve(monkeypatch, b"tampered bytes")

        with pytest.raises(stt_models.ModelDownloadError):
            decoder._download_into(artifact, tmp_path)

        assert target.read_bytes() == b"previous decoder"
        assert _staging_files(tmp_path) == []

    def test_a_transport_error_keeps_the_existing_target(self, monkeypatch, tmp_path):
        artifact = _artifact()
        target = tmp_path / artifact.filename
        target.write_bytes(b"previous decoder")

        def _drop(url, *, write, **kwargs):
            write(b"half a pay")
            raise OSError("connection reset")

        monkeypatch.setattr(decoder, "stream_pinned_payload", _drop)

        with pytest.raises(OSError, match="connection reset"):
            decoder._download_into(artifact, tmp_path)

        assert target.read_bytes() == b"previous decoder"
        assert _staging_files(tmp_path) == []

    def test_a_failed_rename_leaves_no_staging_file(self, monkeypatch, tmp_path):
        artifact = _artifact()
        _serve(monkeypatch, _PAYLOAD)

        def _refuse(src, dst, *a, **k):
            raise PermissionError("target is locked")

        monkeypatch.setattr(decoder, "replace_with_retry", _refuse)

        with pytest.raises(PermissionError):
            decoder._download_into(artifact, tmp_path)

        assert list(tmp_path.iterdir()) == []

    def test_a_verified_payload_replaces_a_stale_target(self, monkeypatch, tmp_path):
        artifact = _artifact()
        target = tmp_path / artifact.filename
        target.write_bytes(b"stale")
        _serve(monkeypatch, _PAYLOAD)

        assert decoder._download_into(artifact, tmp_path) == target
        assert target.read_bytes() == _PAYLOAD
        assert _staging_files(tmp_path) == []

    def test_the_store_fetch_installs_into_store_dir(self, monkeypatch, tmp_path):
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        _serve(monkeypatch, _PAYLOAD)

        assert decoder._fetch_blocking(artifact) == tmp_path / artifact.filename


class TestFailureCode:
    def test_a_refused_payload_is_reported_as_unverified(self):
        exc = stt_models.ModelDownloadError("sha256 mismatch")
        assert decoder._failure_code(exc) == decoder.CODE_PAYLOAD_UNVERIFIED

    @pytest.mark.parametrize("exc", [OSError("disk full"), PermissionError("locked")])
    def test_anything_else_is_an_install_failure(self, exc):
        assert decoder._failure_code(exc) == decoder.CODE_INSTALL_FAILED


# ── the store ────────────────────────────────────────────────────────────


class TestFetch:
    @pytest.mark.asyncio
    async def test_a_verified_fetch_lands_an_executable_at_the_pinned_name(
        self, monkeypatch, tmp_path
    ):
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        _serve(monkeypatch, _PAYLOAD)
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        path = await store.ensure()

        assert path == tmp_path / artifact.filename
        assert path.read_bytes() == _PAYLOAD
        if _pc.IS_POSIX:
            assert stat.S_IMODE(path.stat().st_mode) == 0o755
        assert store.status == {
            "stage": decoder.STAGE_READY,
            "artifact": artifact.filename,
            "downloaded_bytes": artifact.size_bytes,
            "total_bytes": artifact.size_bytes,
            "error_code": "",
            "error_detail": "",
        }
        assert _staging_files(tmp_path) == []

    @pytest.mark.asyncio
    async def test_a_tampered_payload_is_refused_and_leaves_no_file(self, monkeypatch, tmp_path):
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        _serve(monkeypatch, _PAYLOAD + b"tampered")
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        assert await store.ensure() is None

        assert store.status["stage"] == decoder.STAGE_FAILED
        assert store.status["artifact"] == artifact.filename
        assert store.status["error_code"] == decoder.CODE_PAYLOAD_UNVERIFIED
        assert "sha256 mismatch" in str(store.status["error_detail"])
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_a_failed_write_is_reported_as_an_install_failure(self, monkeypatch, tmp_path):
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        _serve(monkeypatch, _PAYLOAD)
        _allow_download(monkeypatch)

        def _refuse(src, dst, *a, **k):
            raise PermissionError("target is locked")

        monkeypatch.setattr(decoder, "replace_with_retry", _refuse)

        store = decoder.DecoderStore()
        assert await store.ensure() is None

        assert store.status["stage"] == decoder.STAGE_FAILED
        assert store.status["error_code"] == decoder.CODE_INSTALL_FAILED
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_an_unsupported_platform_never_reaches_the_network(self, monkeypatch, tmp_path):
        monkeypatch.setattr(decoder, "artifact_for", lambda *a, **k: None)
        monkeypatch.setattr(decoder, "store_dir", lambda: tmp_path)
        _refuse_network(monkeypatch, "an unsupported platform started a download")
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        assert await store.ensure() is None

        assert store.status["stage"] == decoder.STAGE_UNSUPPORTED
        assert store.status["error_code"] == decoder.CODE_UNSUPPORTED

    @pytest.mark.asyncio
    async def test_the_shared_skip_switch_is_honoured(self, monkeypatch, tmp_path):
        """One switch means "this process must not pull artifacts over the network".

        Shared with the whisper weights and the embedding GGUF on purpose, so a
        test run or an air-gapped operator gets it for every subsystem rather than
        for some of them.
        """
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        _refuse_network(monkeypatch, "the skip switch did not stop the download")
        monkeypatch.setenv(stt_models.SKIP_DOWNLOAD_ENV, "1")

        store = decoder.DecoderStore()
        assert await store.ensure() is None

        assert store.status["stage"] == decoder.STAGE_FAILED
        assert store.status["error_code"] == decoder.CODE_DOWNLOAD_DISABLED

    @pytest.mark.asyncio
    async def test_a_present_and_verified_decoder_is_not_re_downloaded(self, monkeypatch, tmp_path):
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        (tmp_path / artifact.filename).write_bytes(_PAYLOAD)
        _refuse_network(monkeypatch, "an already-verified decoder was fetched again")
        # The skip switch stays ON: accepting what is already there needs no network.

        store = decoder.DecoderStore()
        assert await store.ensure() == tmp_path / artifact.filename
        assert store.status["stage"] == decoder.STAGE_READY
        assert store.status["total_bytes"] == artifact.size_bytes

    @pytest.mark.asyncio
    async def test_a_same_size_replacement_on_disk_is_re_fetched(self, monkeypatch, tmp_path):
        """Size is not a trust check, so a same-size overwrite must not be trusted.

        The store directory is writable, so an agent with a shell could drop a
        file of the right length; the digest is what notices, and the fetch then
        replaces it atomically rather than deleting it first.
        """
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        target = tmp_path / artifact.filename
        target.write_bytes(b"#!/bin/sh\nexit 9\n")  # same length, wrong content
        _serve(monkeypatch, _PAYLOAD)
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        assert await store.ensure() == target
        assert target.read_bytes() == _PAYLOAD

    @pytest.mark.asyncio
    async def test_a_mismatched_file_survives_a_failed_re_fetch(self, monkeypatch, tmp_path):
        """A host that cannot reach the release keeps whatever it had."""
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        target = tmp_path / artifact.filename
        target.write_bytes(b"#!/bin/sh\nexit 9\n")
        _serve(monkeypatch, b"captive portal")
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        assert await store.ensure() is None
        assert target.read_bytes() == b"#!/bin/sh\nexit 9\n"
        assert _staging_files(tmp_path) == []

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_transfer(self, monkeypatch, tmp_path):
        """The second caller waits on the lock, then accepts the first one's file."""
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        calls: list[dict] = []
        _serve(monkeypatch, _PAYLOAD, calls)
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        first, second = await asyncio.wait_for(
            asyncio.gather(store.ensure(), store.ensure()), timeout=30
        )

        assert first == second == tmp_path / artifact.filename
        assert len(calls) == 1
        assert store.status["stage"] == decoder.STAGE_READY


class TestVerifiedOnDiskOffLoop:
    """Accepting an installed decoder stats, probes and hashes it off the loop.

    ``_accept_existing``'s size check and ``_verified_on_disk``'s ``is_file``
    probe and digest are all blocking filesystem calls, so each runs in an
    ``asyncio.to_thread`` hop. These tests pin that, and pin that a present, a
    missing and a directory-shaped path keep their existing outcomes.
    """

    @pytest.fixture
    def probe_on_loop(self, monkeypatch):
        """Record, per decoder probe, whether it ran on the main (loop) thread."""
        seen: list[bool] = []
        real_is_file = Path.is_file
        name = _artifact().filename

        def _recording_is_file(self, *args, **kwargs):
            if self.name == name:
                seen.append(threading.current_thread() is threading.main_thread())
            return real_is_file(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_file", _recording_is_file)
        return seen

    @staticmethod
    def _refuse_hash(monkeypatch) -> None:
        def _unexpected(path):
            raise AssertionError(f"hashed a path that is not a regular file: {path}")

        monkeypatch.setattr(decoder, "_sha256_file", _unexpected)

    @pytest.mark.asyncio
    async def test_a_matching_decoder_is_verified_off_loop(
        self, monkeypatch, tmp_path, probe_on_loop
    ):
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        (tmp_path / artifact.filename).write_bytes(_PAYLOAD)

        assert await decoder.DecoderStore()._verified_on_disk(artifact) is True
        assert probe_on_loop == [False]

    @pytest.mark.asyncio
    async def test_a_missing_decoder_is_refused_without_hashing(
        self, monkeypatch, tmp_path, probe_on_loop
    ):
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        self._refuse_hash(monkeypatch)

        assert await decoder.DecoderStore()._verified_on_disk(artifact) is False
        assert probe_on_loop == [False]

    @pytest.mark.asyncio
    async def test_a_directory_at_the_decoder_path_is_refused_without_hashing(
        self, monkeypatch, tmp_path, probe_on_loop
    ):
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        (tmp_path / artifact.filename).mkdir()
        self._refuse_hash(monkeypatch)

        assert await decoder.DecoderStore()._verified_on_disk(artifact) is False
        assert probe_on_loop == [False]

    @pytest.mark.asyncio
    async def test_accepting_an_installed_decoder_checks_its_size_off_loop(
        self, monkeypatch, tmp_path, probe_on_loop
    ):
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        (tmp_path / artifact.filename).write_bytes(_PAYLOAD)
        _refuse_network(monkeypatch, "an already-verified decoder was fetched again")
        size_on_loop: list[bool] = []
        real_is_present = decoder.is_present

        def _recording_is_present(a):
            size_on_loop.append(threading.current_thread() is threading.main_thread())
            return real_is_present(a)

        monkeypatch.setattr(decoder, "is_present", _recording_is_present)

        assert await decoder.DecoderStore().ensure() == tmp_path / artifact.filename
        assert size_on_loop == [False]
        assert probe_on_loop == [False]


# ── the build-time CLI ───────────────────────────────────────────────────


class TestRetiredDecoderMigration:
    """An install that fetched a decoder under an earlier pin keeps working.

    The pins were renamed, so nothing resolves the old file. The transcode
    resolver asks ``migrate_retired_decoder`` once when nothing resolves; it must
    fetch the current pin only for a store-using install that still holds a
    retired file, then remove that file.
    """

    @pytest.fixture(autouse=True)
    def _fresh_process(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(decoder, "_migration_task", None)
        _floor_monkeypatch.setattr(decoder, "bundle_carries_decoder", lambda: False)
        _floor_monkeypatch.setattr(decoder, "_store", decoder.DecoderStore())

    @pytest.mark.asyncio
    async def test_a_retired_file_triggers_one_fetch_and_is_removed(self, monkeypatch, tmp_path):
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        _serve(monkeypatch, _PAYLOAD)
        _allow_download(monkeypatch)
        retired = tmp_path / decoder.RETIRED_FILENAMES[1]
        retired.write_bytes(b"old decoder")

        path = await decoder.migrate_retired_decoder()

        assert path == tmp_path / artifact.filename
        assert path.read_bytes() == _PAYLOAD
        assert not retired.exists()

    @pytest.mark.asyncio
    async def test_it_runs_at_most_once_per_process(self, monkeypatch, tmp_path):
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        _serve(monkeypatch, _PAYLOAD + b"unreachable release")
        _allow_download(monkeypatch)
        (tmp_path / decoder.RETIRED_FILENAMES[0]).write_bytes(b"old decoder")

        assert await decoder.migrate_retired_decoder() is None  # the fetch failed
        _refuse_network(monkeypatch, "a second transcode must not re-attempt the fetch")
        assert await decoder.migrate_retired_decoder() is None

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_transfer(self, monkeypatch, tmp_path):
        """A second transcode during the migration waits for the same fetch."""
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        _allow_download(monkeypatch)
        (tmp_path / decoder.RETIRED_FILENAMES[1]).write_bytes(b"old decoder")
        calls: list[dict] = []
        _serve(monkeypatch, _PAYLOAD, calls)

        first, second = await asyncio.wait_for(
            asyncio.gather(decoder.migrate_retired_decoder(), decoder.migrate_retired_decoder()),
            timeout=30,
        )

        assert first == second == tmp_path / artifact.filename
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_caller_whose_probe_sees_the_migrated_store_joins_the_transfer(
        self, monkeypatch, tmp_path
    ):
        """The second caller's retired-file probe runs only after the first caller's
        migration installed the pin and removed the retired file, so it finds none.
        A migration is under way all the same, and that caller still gets its path."""
        artifact = _artifact()
        _pin(monkeypatch, tmp_path, artifact)
        _allow_download(monkeypatch)
        (tmp_path / decoder.RETIRED_FILENAMES[1]).write_bytes(b"old decoder")
        calls: list[dict] = []
        _serve(monkeypatch, _PAYLOAD, calls)

        migrated = threading.Event()
        probes = 0
        probes_lock = threading.Lock()
        real_probe = decoder.has_retired_decoder

        def _probe() -> bool:
            nonlocal probes
            with probes_lock:
                probes += 1
                later = probes > 1
            if later:
                assert migrated.wait(timeout=10), "the first migration never finished"
            return real_probe()

        shared_store = decoder.store()
        real_ensure = shared_store.ensure

        async def _ensure() -> Path | None:
            try:
                return await real_ensure()
            finally:
                migrated.set()

        monkeypatch.setattr(decoder, "has_retired_decoder", _probe)
        monkeypatch.setattr(shared_store, "ensure", _ensure)

        first, second = await asyncio.wait_for(
            asyncio.gather(decoder.migrate_retired_decoder(), decoder.migrate_retired_decoder()),
            timeout=30,
        )

        assert first == second == tmp_path / artifact.filename
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_no_retired_file_means_no_network(self, monkeypatch, tmp_path):
        _pin(monkeypatch, tmp_path, _artifact())
        _refuse_network(monkeypatch, "nothing to migrate")
        assert await decoder.migrate_retired_decoder() is None

    @pytest.mark.asyncio
    async def test_a_bundled_or_unpinned_install_never_migrates(self, monkeypatch, tmp_path):
        _pin(monkeypatch, tmp_path, _artifact())
        (tmp_path / decoder.RETIRED_FILENAMES[0]).write_bytes(b"old decoder")
        _refuse_network(monkeypatch, "bundled and unpinned installs do not use the store")
        monkeypatch.setattr(decoder, "bundle_carries_decoder", lambda: True)
        assert await decoder.migrate_retired_decoder() is None
        monkeypatch.setattr(decoder, "bundle_carries_decoder", lambda: False)
        monkeypatch.setattr(decoder, "artifact_for", lambda *a, **k: None)
        assert await decoder.migrate_retired_decoder() is None

    def test_retired_names_are_never_current_pins(self):
        assert not set(decoder.RETIRED_FILENAMES) & set(decoder.PACKAGED_FFMPEG_ARTIFACTS)

    @pytest.mark.asyncio
    async def test_the_resolver_retries_after_a_migration(self, monkeypatch):
        """Nothing resolves, the migration installs the pin, the retry finds it."""
        from kiro_crew import transcribe_ffmpeg

        answers = iter([None, "/store/ffmpeg-test"])
        monkeypatch.setattr(transcribe_ffmpeg, "_open_ffmpeg_for_execution", lambda: next(answers))

        async def _migrated():
            return Path("/store/ffmpeg-test")

        monkeypatch.setattr(decoder, "migrate_retired_decoder", _migrated)
        assert await transcribe_ffmpeg._resolve_ffmpeg_for_execution() == "/store/ffmpeg-test"

    @pytest.mark.asyncio
    async def test_the_resolver_does_not_retry_without_a_migration(self, monkeypatch):
        from kiro_crew import transcribe_ffmpeg

        calls: list[int] = []

        def _resolve():
            calls.append(1)

        monkeypatch.setattr(transcribe_ffmpeg, "_open_ffmpeg_for_execution", _resolve)

        async def _nothing():
            return None

        monkeypatch.setattr(decoder, "migrate_retired_decoder", _nothing)
        assert await transcribe_ffmpeg._resolve_ffmpeg_for_execution() is None
        assert calls == [1]


class TestInstallCli:
    """``python -m kiro_crew.stt.decoder --install-into DIR``, the desktop build's path."""

    def test_an_unpinned_platform_exits_3_and_writes_nothing(self, monkeypatch, tmp_path, capsys):
        _refuse_network(monkeypatch, "an unpinned platform started a download")
        target_dir = tmp_path / "bundle"

        code = decoder.main(["--install-into", str(target_dir), "--platform", "macos-x86_64"])

        assert code == 3
        assert "macos-x86_64" in capsys.readouterr().out
        assert not target_dir.exists()

    def test_an_unpinned_host_exits_3_without_a_platform_flag(self, monkeypatch, tmp_path):
        monkeypatch.setattr(decoder, "artifact_for", lambda *a, **k: None)
        _refuse_network(monkeypatch, "an unpinned host started a download")
        target_dir = tmp_path / "bundle"

        assert decoder.main(["--install-into", str(target_dir)]) == 3
        assert not target_dir.exists()

    def test_a_refused_payload_exits_1(self, monkeypatch, tmp_path, capsys):
        _serve(monkeypatch, b"not the pinned bytes")
        target_dir = tmp_path / "bundle"

        code = decoder.main(["--install-into", str(target_dir), "--platform", "linux-x86_64"])

        assert code == 1
        assert "decoder download failed" in capsys.readouterr().err
        assert list(target_dir.iterdir()) == []

    def test_a_pinned_platform_installs_and_prints_the_path(self, monkeypatch, tmp_path, capsys):
        """The named platform's pin, not the host's, decides what is fetched."""
        artifact = next(a for a in decoder.ARTIFACTS if a.platform_key == "linux-aarch64")
        calls: list[dict] = []

        def _stream(url, *, label, expected_size, expected_sha256, write, **kwargs):
            calls.append({"url": url, "size": expected_size, "sha256": expected_sha256})
            write(b"stand-in bytes")

        monkeypatch.setattr(decoder, "stream_pinned_payload", _stream)
        target_dir = tmp_path / "bundle"

        code = decoder.main(["--install-into", str(target_dir), "--platform", "linux-aarch64"])

        assert code == 0
        installed = target_dir / artifact.filename
        assert capsys.readouterr().out.strip() == str(installed)
        assert installed.read_bytes() == b"stand-in bytes"
        assert calls == [
            {"url": artifact.url, "size": artifact.size_bytes, "sha256": artifact.sha256}
        ]
        assert _staging_files(target_dir) == []
        if _pc.IS_POSIX:
            assert os.access(installed, os.X_OK)


# ── the trigger after a model download ───────────────────────────────────


class TestAutofetchTrigger:
    @pytest.mark.asyncio
    async def test_a_bundled_interpreter_never_fetches_a_second_decoder(self, monkeypatch):
        """A desktop release repairs itself by being reinstalled, not by downloading.

        Its resolver deliberately never looks outside its own payload, so a fetch
        there spends the operator's bandwidth on a file nothing can use.
        """
        monkeypatch.setattr(decoder.platform_compat, "is_bundled_interpreter", lambda: True)

        def _unexpected():
            raise AssertionError("a bundled install probed for a system decoder")

        monkeypatch.setattr(transcribe, "_find_ffmpeg", _unexpected)
        await decoder.maybe_autofetch()

    @pytest.mark.asyncio
    async def test_a_host_that_already_has_a_decoder_fetches_nothing(self, monkeypatch):
        monkeypatch.setattr(decoder.platform_compat, "is_bundled_interpreter", lambda: False)
        monkeypatch.setattr(transcribe, "_find_ffmpeg", lambda: "/usr/local/bin/ffmpeg")
        monkeypatch.setattr(
            decoder, "store", lambda: pytest.fail("a host with ffmpeg started a fetch")
        )
        await decoder.maybe_autofetch()

    @pytest.mark.asyncio
    async def test_a_source_install_without_a_decoder_fetches_one(self, monkeypatch):
        monkeypatch.setattr(decoder.platform_compat, "is_bundled_interpreter", lambda: False)
        monkeypatch.setattr(transcribe, "_find_ffmpeg", lambda: None)
        calls: list[str] = []

        class _Store:
            async def ensure(self):
                calls.append("ensure")
                return None

        monkeypatch.setattr(decoder, "store", _Store)
        await decoder.maybe_autofetch()
        assert calls == ["ensure"]

    @pytest.mark.asyncio
    async def test_a_completed_model_download_starts_the_decoder_fetch(self, monkeypatch, tmp_path):
        """The one moment a second transfer reads as part of the same setup.

        Detached rather than awaited, so the test drains the store's own task set
        instead of assuming the work finished inside `ensure`.
        """
        payload = b"weights"
        model = stt_models.WhisperModel("stub", len(payload), hashlib.sha256(payload).hexdigest())
        monkeypatch.setattr(stt_models, "models_dir", lambda: tmp_path)
        monkeypatch.setattr(
            stt_models, "_download_blocking", lambda m, *a, **k: stt_models.model_path(m)
        )
        stt_models.model_path(model).write_bytes(payload)
        started: list[str] = []

        async def _fake_autofetch() -> None:
            started.append("autofetch")

        monkeypatch.setattr(decoder, "maybe_autofetch", _fake_autofetch)
        store = stt_models.ModelStore()
        # `_accept_existing` would short-circuit a present file, which is not the
        # branch under test: force the download path.
        monkeypatch.setattr(stt_models, "is_present", lambda _m: False)
        _allow_download(monkeypatch)

        await store.ensure(model)
        for task in list(store._decoder_tasks):
            await task

        assert started == ["autofetch"]

    @pytest.mark.asyncio
    async def test_a_failing_autofetch_does_not_surface_as_an_unretrieved_exception(
        self, monkeypatch, tmp_path
    ):
        """The model download must still report success when the decoder fetch dies."""
        payload = b"weights"
        model = stt_models.WhisperModel("stub", len(payload), hashlib.sha256(payload).hexdigest())
        monkeypatch.setattr(stt_models, "models_dir", lambda: tmp_path)
        monkeypatch.setattr(
            stt_models, "_download_blocking", lambda m, *a, **k: stt_models.model_path(m)
        )
        stt_models.model_path(model).write_bytes(payload)

        async def _explode() -> None:
            raise RuntimeError("no network")

        monkeypatch.setattr(decoder, "maybe_autofetch", _explode)
        monkeypatch.setattr(stt_models, "is_present", lambda _m: False)
        _allow_download(monkeypatch)

        store = stt_models.ModelStore()
        assert await store.ensure(model) == stt_models.model_path(model)
        for task in list(store._decoder_tasks):
            with pytest.raises(RuntimeError):
                await task
        assert store.status["step"] == "ready"


# ── the resolver ─────────────────────────────────────────────────────────


class TestResolver:
    """What ``transcribe`` will and will not execute out of the store.

    The store directory is user-writable, so its PATH vouches for nothing. What is
    accepted is a pinned filename whose bytes match that pin, re-verified on every
    open exactly as a bundled payload is.
    """

    @staticmethod
    def _install(monkeypatch, tmp_path: Path, payload: bytes) -> Path:
        filename = "ffmpeg-store-test.exe" if _pc.IS_WINDOWS else "ffmpeg-store-test"
        target = tmp_path / filename
        target.write_bytes(payload)
        target.chmod(0o755)
        monkeypatch.setattr(decoder, "store_dir", lambda: tmp_path)
        monkeypatch.setattr(
            transcribe,
            "_PACKAGED_FFMPEG_ARTIFACTS",
            {filename: (len(payload), hashlib.sha256(payload).hexdigest())},
        )
        monkeypatch.setattr(transcribe, "_SIGNER_REWRITTEN_FFMPEG_ARTIFACTS", frozenset())
        monkeypatch.setattr(transcribe.platform_compat, "is_bundled_interpreter", lambda: False)
        monkeypatch.setattr(transcribe, "_find_system_ffmpeg", lambda: None)
        return target

    def test_a_fetched_decoder_is_what_the_resolver_reports(self, monkeypatch, tmp_path):
        target = self._install(monkeypatch, tmp_path, b"#!/bin/sh\nexit 0\n")
        assert transcribe._store_ffmpeg() == str(target)
        assert transcribe._find_ffmpeg() == str(target)
        assert transcribe.ffmpeg_source() == transcribe.FFMPEG_SOURCE_STORE

    def test_a_tampered_store_file_is_ignored_rather_than_executed(self, monkeypatch, tmp_path):
        """Same size, different bytes: the digest is the only thing standing here."""
        target = self._install(monkeypatch, tmp_path, b"#!/bin/sh\nexit 0\n")
        target.write_bytes(b"#!/bin/sh\nexit 9\n")

        assert transcribe._store_ffmpeg() is None
        assert transcribe._find_ffmpeg() is None
        assert transcribe.ffmpeg_source() is None
        assert transcribe._open_store_ffmpeg_resource() is None

    def test_an_unpinned_filename_in_the_store_is_not_a_decoder(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path, b"#!/bin/sh\nexit 0\n")
        stranger = tmp_path / "ffmpeg"
        stranger.write_bytes(b"#!/bin/sh\nexit 0\n")
        stranger.chmod(0o755)
        # The pinned file still resolves; the unpinned name beside it is invisible,
        # because the lookup is by pinned FILENAME, never by "anything executable
        # in this directory".
        assert transcribe._store_ffmpeg() is not None

    def test_a_system_decoder_is_preferred_over_the_store(self, monkeypatch, tmp_path):
        """A package manager's copy is the one the host's own updates keep current."""
        self._install(monkeypatch, tmp_path, b"#!/bin/sh\nexit 0\n")
        monkeypatch.setattr(transcribe, "_find_system_ffmpeg", lambda: "/usr/local/bin/ffmpeg")

        assert transcribe._find_ffmpeg() == "/usr/local/bin/ffmpeg"
        assert transcribe.ffmpeg_source() == transcribe.FFMPEG_SOURCE_SYSTEM
        assert transcribe._open_ffmpeg_for_execution() == "/usr/local/bin/ffmpeg"

    def test_a_bundled_install_never_falls_through_to_the_store(self, monkeypatch, tmp_path):
        """A desktop release fails closed instead of running unbundled bytes."""
        self._install(monkeypatch, tmp_path, b"#!/bin/sh\nexit 0\n")
        monkeypatch.setattr(transcribe.platform_compat, "is_bundled_interpreter", lambda: True)
        monkeypatch.setattr(transcribe, "_trusted_site_package_roots", lambda: ())

        assert transcribe._find_ffmpeg() is None
        assert transcribe._open_ffmpeg_for_execution() is None

    def test_the_store_is_never_added_to_the_candidate_directory_list(self):
        """That list is searched by NAME, so an entry there is a path claim.

        The store is reached by pinned filename plus digest instead, which is the
        distinction that keeps this from widening what the gateway will execute.
        """
        store = str(decoder.store_dir())
        for directory in transcribe._ffmpeg_candidate_dirs():
            assert directory != store
        assert store not in transcribe._FFMPEG_CANDIDATE_DIRS

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="the shell-script stand-in is POSIX")
    def test_the_authenticated_store_handle_actually_executes(self, monkeypatch, tmp_path):
        """The resolver hands back bytes that run, not merely a path that exists."""
        script = b"#!/bin/sh\necho store-decoder\n"
        self._install(monkeypatch, tmp_path, script)

        opened = transcribe._open_store_ffmpeg_resource()

        assert opened is not None
        try:
            result = transcribe.subprocess.run(
                [opened.execution_path],
                check=False,
                capture_output=True,
                pass_fds=(opened.descriptor,),
            )
        finally:
            opened.close()
        assert result.returncode == 0
        assert result.stdout.strip() == b"store-decoder"

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="POSIX exec bit")
    def test_a_non_executable_store_file_is_skipped(self, monkeypatch, tmp_path):
        target = self._install(monkeypatch, tmp_path, b"#!/bin/sh\nexit 0\n")
        os.chmod(target, 0o644)
        assert transcribe._store_ffmpeg() is None

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="POSIX symlink semantics")
    def test_a_symlink_out_of_the_store_is_refused(self, monkeypatch, tmp_path):
        """The digest would otherwise describe bytes at a path nobody vouched for."""
        outside = tmp_path / "outside"
        outside.mkdir()
        payload = b"#!/bin/sh\nexit 0\n"
        real = outside / "real-ffmpeg"
        real.write_bytes(payload)
        real.chmod(0o755)
        store = tmp_path / "store"
        store.mkdir()
        filename = "ffmpeg-store-test"
        (store / filename).symlink_to(real)
        monkeypatch.setattr(decoder, "store_dir", lambda: store)
        monkeypatch.setattr(
            transcribe,
            "_PACKAGED_FFMPEG_ARTIFACTS",
            {filename: (len(payload), hashlib.sha256(payload).hexdigest())},
        )
        monkeypatch.setattr(transcribe, "_SIGNER_REWRITTEN_FFMPEG_ARTIFACTS", frozenset())
        monkeypatch.setattr(transcribe.platform_compat, "is_bundled_interpreter", lambda: False)
        monkeypatch.setattr(transcribe, "_find_system_ffmpeg", lambda: None)

        assert transcribe._store_ffmpeg() is None

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="POSIX symlink semantics")
    def test_a_symlinked_ancestor_of_the_store_still_resolves(self, monkeypatch, tmp_path):
        """The complement of the refusal above, and the distinction is the whole point.

        A symlink AT the pinned filename is refused, because the digest would then
        describe bytes at a path nobody vouched for. A symlink somewhere ABOVE the
        store is just how the host spells that directory -- /home -> /var/home on an
        rpm-ostree distribution, /tmp -> /private/tmp on macOS -- and refusing it
        would report the decoder this store installed and verified as permanently
        absent on exactly the hosts this feature exists for.
        """
        real_home = tmp_path / "var-home"
        real_home.mkdir()
        store = real_home / "store"
        store.mkdir()
        linked_home = tmp_path / "home"
        linked_home.symlink_to(real_home)

        payload = b"#!/bin/sh\nexit 0\n"
        filename = "ffmpeg-store-test"
        (store / filename).write_bytes(payload)
        (store / filename).chmod(0o755)

        # The store reports the path THROUGH the symlink, which is what a data home
        # derived from the user's home directory looks like on such a host.
        monkeypatch.setattr(decoder, "store_dir", lambda: linked_home / "store")
        monkeypatch.setattr(
            transcribe,
            "_PACKAGED_FFMPEG_ARTIFACTS",
            {filename: (len(payload), hashlib.sha256(payload).hexdigest())},
        )
        monkeypatch.setattr(transcribe, "_SIGNER_REWRITTEN_FFMPEG_ARTIFACTS", frozenset())
        monkeypatch.setattr(transcribe.platform_compat, "is_bundled_interpreter", lambda: False)
        monkeypatch.setattr(transcribe, "_find_system_ffmpeg", lambda: None)

        assert transcribe._store_ffmpeg() == str(store / filename)
        assert transcribe.ffmpeg_source() == transcribe.FFMPEG_SOURCE_STORE
