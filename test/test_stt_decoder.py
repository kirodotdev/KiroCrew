"""The digest-verified ffmpeg decoder store (``kiro_crew.stt.decoder``).

Every test here is offline: the network fetch is replaced at
``models.stream_pinned_payload``, which is the ONE seam both the whisper weights
and this wheel go through, and every write lands under ``tmp_path`` because
``store_dir`` is pinned per test. Nothing spawns a process except the resolver
test, which authenticates a tiny shell script standing in for the real 80 MB
executable — the pin is monkeypatched to that script's own digest, so the check
under test is the real one.

The suite's conftest sets ``KIROCREW_SKIP_MODEL_DOWNLOAD=1`` for every test, so a
test that exercises the fetch has to unset it deliberately; that switch has its
own test below.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import zipfile
from pathlib import Path

import pytest

from kiro_crew import platform_compat as _pc
from kiro_crew import transcribe
from kiro_crew.stt import decoder
from kiro_crew.stt import models as stt_models

# ── helpers ──────────────────────────────────────────────────────────────


def _artifact(
    payload: bytes, *, filename: str = "ffmpeg-test-v1", wheel_body: bytes = b""
) -> decoder.DecoderArtifact:
    """A pinned artifact whose digests describe *payload* and *wheel_body*."""
    return decoder.DecoderArtifact(
        platform_key="test-arch",
        filename=filename,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        wheel_filename="imageio_ffmpeg-0.6.0-py3-none-test.whl",
        wheel_url="https://files.pythonhosted.org/packages/aa/bb/imageio_ffmpeg-test.whl",
        wheel_size_bytes=len(wheel_body),
        wheel_sha256=hashlib.sha256(wheel_body).hexdigest(),
    )


def _wheel_bytes(members: dict[str, bytes]) -> bytes:
    """A zip carrying exactly *members*, keyed by full archive name."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, body in members.items():
            bundle.writestr(name, body)
    return buffer.getvalue()


def _pin(monkeypatch, tmp_path: Path, artifact: decoder.DecoderArtifact) -> None:
    """Make *artifact* this host's decoder and *tmp_path* the store."""
    monkeypatch.setattr(decoder, "artifact_for", lambda *a, **k: artifact)
    monkeypatch.setattr(decoder, "store_dir", lambda: tmp_path)


def _serve(monkeypatch, body: bytes) -> None:
    """Answer the store's one network call with *body*, offline.

    Patched at the shared streamer rather than at ``urlopen`` so the test states
    which seam it stands in for; the streamer's own pin enforcement has its own
    coverage in ``test_stt_engine.py``. Substituted on ``decoder``'s OWN global,
    which is the name the fetch reads -- a from-import means patching the defining
    module would not be seen here.
    """

    def _stream(url, *, label, expected_size, expected_sha256, write, **kwargs):
        assert url.startswith("https://"), url
        digest = hashlib.sha256(body).hexdigest()
        if len(body) != expected_size or digest != expected_sha256:
            raise stt_models.ModelDownloadError(
                f"{label}: sha256 mismatch (got {digest[:16]}…, expected {expected_sha256[:16]}…)"
            )
        write(body)

    monkeypatch.setattr(decoder, "stream_pinned_payload", _stream)


def _allow_download(monkeypatch) -> None:
    monkeypatch.delenv(stt_models.SKIP_DOWNLOAD_ENV, raising=False)


# ── platform selection ───────────────────────────────────────────────────


class TestPlatformSelection:
    """Which pinned artifact a host resolves to, and which hosts resolve to none.

    Parametrised over the reported ``system``/``machine`` because the SAME
    hardware reports different strings per OS — Windows says ``AMD64`` and macOS
    ``arm64`` where Linux says ``x86_64`` and ``aarch64`` — so matching on the raw
    value would leave two of the five shipped platforms unable to find their pin.
    """

    @pytest.mark.parametrize(
        ("system", "machine", "expected"),
        [
            ("Linux", "x86_64", "ffmpeg-linux-x86_64-v7.0.2"),
            ("Linux", "AMD64", "ffmpeg-linux-x86_64-v7.0.2"),
            ("Linux", "aarch64", "ffmpeg-linux-aarch64-v7.0.2"),
            ("Linux", "arm64", "ffmpeg-linux-aarch64-v7.0.2"),
            ("Darwin", "arm64", "ffmpeg-macos-aarch64-v7.1"),
            ("Darwin", "x86_64", "ffmpeg-macos-x86_64-v7.1"),
            ("Windows", "AMD64", "ffmpeg-win-x86_64-v7.1.exe"),
            ("Windows", "x86_64", "ffmpeg-win-x86_64-v7.1.exe"),
        ],
    )
    def test_a_shipped_platform_resolves_its_pinned_artifact(self, system, machine, expected):
        artifact = decoder.artifact_for(system, machine)
        assert artifact is not None
        assert artifact.filename == expected

    @pytest.mark.parametrize(
        ("system", "machine"),
        [
            ("Linux", "armv7l"),  # 32-bit ARM: no upstream wheel
            ("Linux", "s390x"),
            ("Windows", "ARM64"),  # no win-arm64 artifact upstream
            ("Windows", "i686"),  # win32 is deliberately not shipped
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

    def test_every_pinned_artifact_is_a_shipped_desktop_platform(self):
        """The store may not offer a platform the desktop matrix does not ship.

        ``transcribe._SHIPPED_FFMPEG_PLATFORMS`` is the maintainer-owned set that
        test_transcribe.py already cross-checks against upstream's own filename
        table, so tying to it means one edit (adding a build leg) cannot leave the
        store fetching bytes nothing else authenticates.
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

    def test_the_store_lives_under_the_write_protected_models_tree(self, tmp_path, monkeypatch):
        """A sibling of the whisper weights, so both share one fenced directory."""
        monkeypatch.setattr(decoder, "models_dir", lambda: tmp_path / "models" / "whisper")
        assert decoder.store_dir() == tmp_path / "models" / "ffmpeg"


# ── the fetch ────────────────────────────────────────────────────────────


class TestFetch:
    @pytest.mark.asyncio
    async def test_a_verified_fetch_lands_an_executable_at_the_pinned_name(
        self, monkeypatch, tmp_path
    ):
        payload = b"#!/bin/sh\nexit 0\n"
        artifact = _artifact(payload)
        wheel = _wheel_bytes({artifact.member: payload})
        artifact = _artifact(payload, wheel_body=wheel)
        _pin(monkeypatch, tmp_path, artifact)
        _serve(monkeypatch, wheel)
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        path = await store.ensure()

        assert path == tmp_path / artifact.filename
        assert path.read_bytes() == payload
        if _pc.IS_POSIX:
            assert stat.S_IMODE(path.stat().st_mode) == 0o755
        assert store.status["stage"] == decoder.STAGE_READY
        assert store.status["error_code"] == ""
        # Neither staging file survives: a leftover `.part` is a partial payload
        # sitting in a directory the resolver enumerates.
        assert not list(tmp_path.glob("*.part"))

    @pytest.mark.asyncio
    async def test_a_tampered_wheel_is_refused_and_leaves_no_file(self, monkeypatch, tmp_path):
        """The wheel pin bounds the network fetch, before the zip parser runs."""
        payload = b"#!/bin/sh\nexit 0\n"
        real_wheel = _wheel_bytes({_artifact(payload).member: payload})
        artifact = _artifact(payload, wheel_body=real_wheel)
        _pin(monkeypatch, tmp_path, artifact)
        _serve(monkeypatch, real_wheel + b"tampered")
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        assert await store.ensure() is None

        assert store.status["stage"] == decoder.STAGE_FAILED
        assert store.status["error_code"] == decoder.CODE_WHEEL_UNVERIFIED
        assert "sha256 mismatch" in str(store.status["error_detail"])
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_a_wheel_without_the_pinned_member_is_refused(self, monkeypatch, tmp_path):
        payload = b"#!/bin/sh\nexit 0\n"
        wheel = _wheel_bytes({"imageio_ffmpeg/binaries/ffmpeg-other-v9": payload})
        artifact = _artifact(payload, wheel_body=wheel)
        _pin(monkeypatch, tmp_path, artifact)
        _serve(monkeypatch, wheel)
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        assert await store.ensure() is None

        assert store.status["error_code"] == decoder.CODE_MEMBER_MISSING
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_a_zip_slip_member_cannot_choose_where_the_write_lands(
        self, monkeypatch, tmp_path
    ):
        """A traversal name must be refused, and must not write outside the store.

        ``ZipFile.extract`` derives its destination from the member NAME, which is
        data inside the archive; this store looks the member up by its full name
        and writes through its own descriptor, so a crafted entry is simply not
        the member asked for.
        """
        payload = b"#!/bin/sh\nexit 0\n"
        store_dir = tmp_path / "store"
        store_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        artifact = _artifact(payload)
        wheel = _wheel_bytes(
            {
                f"imageio_ffmpeg/binaries/../../../outside/{artifact.filename}": payload,
                f"../../{artifact.filename}": payload,
            }
        )
        artifact = _artifact(payload, wheel_body=wheel)
        _pin(monkeypatch, store_dir, artifact)
        _serve(monkeypatch, wheel)
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        assert await store.ensure() is None

        assert store.status["error_code"] == decoder.CODE_MEMBER_MISSING
        assert list(outside.iterdir()) == [], "a traversal member escaped the store"
        assert list(store_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_extracted_bytes_that_fail_the_pin_are_refused(self, monkeypatch, tmp_path):
        """The wheel digest matching says nothing about the member inside it.

        Reached by pinning the executable to bytes the archive does not carry, so
        the wheel is accepted and the SECOND check is the one that fires.
        """
        payload = b"#!/bin/sh\nexit 0\n"
        member = "imageio_ffmpeg/binaries/ffmpeg-test-v1"
        # Same LENGTH, different content: the size pre-check passes, so the digest
        # is what has to catch it.
        wheel = _wheel_bytes({member: b"#!/bin/sh\nexit 9\n"})
        artifact = _artifact(payload, wheel_body=wheel)
        _pin(monkeypatch, tmp_path, artifact)
        _serve(monkeypatch, wheel)
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        assert await store.ensure() is None

        assert store.status["error_code"] == decoder.CODE_PAYLOAD_UNVERIFIED
        assert "sha256 mismatch" in str(store.status["error_detail"])
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_a_member_larger_than_the_pin_stops_before_filling_the_disk(
        self, monkeypatch, tmp_path
    ):
        """A decompressed member's real length is bounded by nothing in the header."""
        payload = b"x" * 32
        wheel = _wheel_bytes({"imageio_ffmpeg/binaries/ffmpeg-test-v1": b"x" * 4096})
        artifact = _artifact(payload, wheel_body=wheel)
        _pin(monkeypatch, tmp_path, artifact)
        _serve(monkeypatch, wheel)
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        assert await store.ensure() is None

        assert store.status["error_code"] == decoder.CODE_PAYLOAD_UNVERIFIED
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_an_unsupported_platform_never_reaches_the_network(self, monkeypatch, tmp_path):
        monkeypatch.setattr(decoder, "artifact_for", lambda *a, **k: None)
        monkeypatch.setattr(decoder, "store_dir", lambda: tmp_path)

        def _unexpected(*a, **k):
            raise AssertionError("an unsupported platform started a download")

        monkeypatch.setattr(decoder, "stream_pinned_payload", _unexpected)
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
        payload = b"#!/bin/sh\nexit 0\n"
        wheel = _wheel_bytes({_artifact(payload).member: payload})
        artifact = _artifact(payload, wheel_body=wheel)
        _pin(monkeypatch, tmp_path, artifact)

        def _unexpected(*a, **k):
            raise AssertionError("the skip switch did not stop the download")

        monkeypatch.setattr(decoder, "stream_pinned_payload", _unexpected)
        monkeypatch.setenv(stt_models.SKIP_DOWNLOAD_ENV, "1")

        store = decoder.DecoderStore()
        assert await store.ensure() is None

        assert store.status["error_code"] == decoder.CODE_DOWNLOAD_DISABLED

    @pytest.mark.asyncio
    async def test_a_present_and_verified_decoder_is_not_re_downloaded(self, monkeypatch, tmp_path):
        payload = b"#!/bin/sh\nexit 0\n"
        artifact = _artifact(payload)
        _pin(monkeypatch, tmp_path, artifact)
        (tmp_path / artifact.filename).write_bytes(payload)

        def _unexpected(*a, **k):
            raise AssertionError("an already-verified decoder was fetched again")

        monkeypatch.setattr(decoder, "stream_pinned_payload", _unexpected)
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        assert await store.ensure() == tmp_path / artifact.filename
        assert store.status["stage"] == decoder.STAGE_READY

    @pytest.mark.asyncio
    async def test_a_same_size_replacement_on_disk_is_re_fetched(self, monkeypatch, tmp_path):
        """Size is not a trust check, so a same-size overwrite must not be trusted.

        The store directory is writable, so an agent with a shell could drop a
        file of the right length; the digest is what notices, and the fetch then
        replaces it atomically rather than deleting it first (a host that cannot
        reach PyPI keeps whatever it had).
        """
        payload = b"#!/bin/sh\nexit 0\n"
        artifact = _artifact(payload)
        wheel = _wheel_bytes({artifact.member: payload})
        artifact = _artifact(payload, wheel_body=wheel)
        _pin(monkeypatch, tmp_path, artifact)
        target = tmp_path / artifact.filename
        target.write_bytes(b"#!/bin/sh\nexit 9\n")  # same length, wrong content
        _serve(monkeypatch, wheel)
        _allow_download(monkeypatch)

        store = decoder.DecoderStore()
        assert await store.ensure() == target
        assert target.read_bytes() == payload


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
