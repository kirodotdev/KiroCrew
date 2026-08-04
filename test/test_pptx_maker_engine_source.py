"""Tests for the digest-pinned engine fetch (`backend/engine_source.py`).

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

The engine is third-party code fetched at runtime and then BUILT and EXECUTED
(``uv sync`` compiles wheels; the app's agents drive the engine), so the two
things that decide whether unvetted code runs are pinned hard here:

1. the **sha256 over the received bytes** — a mismatch must refuse, and must do
   so before anything is extracted, built or run;
2. **safe extraction** — ``tarfile.extractall`` writes wherever a member name
   points, so a traversal, absolute, symlink or device member must be refused.

No test reaches the network: the download is either mocked or the archive is a
tarball this file builds itself.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew.apps.builtins.pptx_maker.backend import engine_source


def _tar_bytes(entries: list[tarfile.TarInfo], payloads: dict[str, bytes]) -> bytes:
    """Build a ``.tar.gz`` from explicit TarInfo members. Used for hostile tars."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info in entries:
            data = payloads.get(info.name, b"")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _benign_engine_tar() -> bytes:
    """A tarball shaped like the real one: one wrapper dir containing mcp-local."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        wrapper = tarfile.TarInfo("repo-abc123")
        wrapper.type = tarfile.DIRTYPE
        wrapper.mode = 0o755
        tar.addfile(wrapper)
        mcp = tarfile.TarInfo("repo-abc123/mcp-local")
        mcp.type = tarfile.DIRTYPE
        mcp.mode = 0o755
        tar.addfile(mcp)
        payload = b"[project]\nname = 'sdpm-mcp-local'\n"
        f = tarfile.TarInfo("repo-abc123/mcp-local/pyproject.toml")
        f.size = len(payload)
        f.mode = 0o644
        tar.addfile(f, io.BytesIO(payload))
    return buf.getvalue()


class TestThePin:
    def test_the_commit_is_a_full_sha(self):
        """A short sha is ambiguous and an abbreviation can become non-unique."""
        assert len(engine_source.ENGINE_COMMIT) == 40
        assert all(c in "0123456789abcdef" for c in engine_source.ENGINE_COMMIT)

    def test_the_digest_is_a_full_sha256(self):
        """The trust anchor: a truncated digest would weaken it silently."""
        assert len(engine_source.ENGINE_TARBALL_SHA256) == 64
        assert all(c in "0123456789abcdef" for c in engine_source.ENGINE_TARBALL_SHA256)

    def test_the_archive_url_is_https_and_names_the_commit(self):
        """Fetched by COMMIT, never by tag: a tag is a mutable ref, and this URL
        is the only thing that decides which tree arrives."""
        url = engine_source.archive_url()
        assert url.startswith("https://")
        assert engine_source.ENGINE_COMMIT in url
        assert engine_source.ENGINE_TAG not in url

    def test_the_tag_is_display_only(self):
        """`ENGINE_TAG` must not be able to influence the fetch — it exists so
        the UI can show a version, and it is only ever reported for a tree whose
        digest matched."""
        assert engine_source.ENGINE_TAG not in engine_source.archive_url()


class TestUrlOverride:
    def test_an_https_override_is_honoured(self, monkeypatch: pytest.MonkeyPatch):
        """For a mirrored / air-gapped deployment. The digest still gates it."""
        monkeypatch.setenv(engine_source.ENGINE_URL_ENV, "https://mirror.example/e.tar.gz")
        assert engine_source.archive_url() == "https://mirror.example/e.tar.gz"

    @pytest.mark.parametrize(
        "bad", ["http://mirror.example/e.tar.gz", "file:///etc/passwd", "ftp://x/y"]
    )
    def test_a_non_https_override_is_refused(self, bad: str, monkeypatch: pytest.MonkeyPatch):
        """An operator-supplied value must not be able to read local files or
        fetch plaintext."""
        monkeypatch.setenv(engine_source.ENGINE_URL_ENV, bad)
        assert engine_source.archive_url().startswith("https://github.com/")

    def test_redaction_drops_userinfo_query_and_path(self):
        """A mirror override may carry credentials, a signed query, or a PATH token."""
        redacted = engine_source.redact_url("https://u:p@host/path?sig=secret#frag")
        assert "secret" not in redacted and "p@" not in redacted
        # HOST-ONLY. The path used to be kept, and it is the third place a mirror can
        # hide a secret — `/artifactory/api/npm/tok-…/`, a presigned `/AKIA…/`.
        assert redacted == "https://host"

    def test_a_path_embedded_credential_is_redacted(self):
        for url, secret in (
            ("https://artifactory.example/api/npm/tok-9f3b2c/e.tar.gz", "tok-9f3b2c"),
            ("https://mirror.example/AKIAIOSFODNN7EXAMPLE/e.tar.gz", "AKIAIOSFODNN7EXAMPLE"),
        ):
            assert secret not in engine_source.redact_url(url)

    def test_the_port_is_kept(self):
        """Host-only, not scheme-only: an air-gapped mirror is often on a non-default
        port, and a port holds no secret."""
        assert engine_source.redact_url("https://mirror.example:8443/x/e.tar.gz") == (
            "https://mirror.example:8443"
        )


class TestDownloadVerification:
    """The whole security story: bytes are trusted only after they hash right."""

    def _fake_response(self, data: bytes):
        response = mock.MagicMock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        chunks = [data, b""]
        response.read = mock.Mock(side_effect=chunks)
        response.headers = {"Content-Length": str(len(data))}
        return response

    def _patch_download(self, data: bytes):
        opener = mock.Mock()
        opener.open = mock.Mock(return_value=self._fake_response(data))
        return mock.patch.object(engine_source.urllib.request, "build_opener", return_value=opener)

    def _raise_on_open(self, exc: Exception):
        """Make `opener.open(...)` raise — the call INSIDE the guarded block.

        Patching `build_opener` instead puts the raise above the `try`, so the handler
        under test never runs.
        """
        opener = mock.Mock()
        opener.open = mock.Mock(side_effect=exc)
        return mock.patch.object(
            engine_source.urllib.request, "build_opener", return_value=opener
        )

    _SIGNED = "https://mirror.example/engine.tar.gz?X-Amz-Signature=sup3rsecret"

    def test_a_download_error_does_not_echo_the_signed_url(self, tmp_path: Path):
        """A mirror override may hold credentials in userinfo OR a signed query.

        `redact_url` exists for exactly this and the log line already used it — but the
        error RETURN interpolated `{exc}`, and this value reaches the dashboard. Same
        omission, same fix, as `papyrus/backend/tectonic.py`'s download path.
        """
        staging = tmp_path / "engine.tar.gz"
        with (
            mock.patch.object(engine_source, "archive_url", return_value=self._SIGNED),
            self._raise_on_open(OSError(f"connect failed to {self._SIGNED}")),
        ):
            ok, error = engine_source.download_archive(staging)
        assert ok is False
        assert "sup3rsecret" not in error, f"signed query leaked: {error}"

    def test_a_malformed_url_is_handled_not_escaped(self, tmp_path: Path):
        """`http.client.InvalidURL` derives from `HTTPException`, NOT from `OSError`,
        `URLError` or `ValueError` — so it used to pass through this handler entirely
        and be reported by an outer catch-all as an unexplained crash."""
        import http.client

        staging = tmp_path / "engine.tar.gz"
        with (
            mock.patch.object(engine_source, "archive_url", return_value=self._SIGNED),
            self._raise_on_open(
                http.client.InvalidURL("nonnumeric port: 'sup3rsecret@mirror.example'")
            ),
        ):
            ok, error = engine_source.download_archive(staging)
        assert ok is False
        assert "sup3rsecret" not in error, f"credential leaked: {error}"
        # Reported accurately rather than as a crash, and the redacted host survives.
        assert "InvalidURL" in error
        assert "mirror.example" in error

    def test_a_matching_digest_is_accepted(self, tmp_path: Path):
        payload = b"x" * 200_000
        staging = tmp_path / "engine.tar.gz"
        with (
            self._patch_download(payload),
            mock.patch.object(
                engine_source, "ENGINE_TARBALL_SHA256", hashlib.sha256(payload).hexdigest()
            ),
        ):
            ok, error = engine_source.download_archive(staging)
        assert ok is True and error == ""
        assert staging.read_bytes() == payload

    def test_a_digest_mismatch_refuses_and_removes_the_bytes(self, tmp_path: Path):
        """The load-bearing refusal. A tampered / re-generated archive must not
        be left on disk for a later step or a retry to pick up."""
        staging = tmp_path / "engine.tar.gz"
        with self._patch_download(b"y" * 200_000):
            ok, error = engine_source.download_archive(staging)
        assert ok is False
        assert "REFUSING" in error and "sha256" in error
        assert engine_source.ENGINE_TARBALL_SHA256[:16] in error
        assert not staging.exists(), "refused bytes must not survive"

    def test_a_truncated_transfer_is_refused_even_if_it_hashed(self, tmp_path: Path):
        """Belt and braces: a tiny body is an error page, not a source tree."""
        payload = b"404: Not Found"
        staging = tmp_path / "engine.tar.gz"
        with (
            self._patch_download(payload),
            mock.patch.object(
                engine_source, "ENGINE_TARBALL_SHA256", hashlib.sha256(payload).hexdigest()
            ),
        ):
            ok, error = engine_source.download_archive(staging)
        assert ok is False and "implausibly small" in error
        assert not staging.exists()

    def test_a_network_failure_is_reported_not_raised(self, tmp_path: Path):
        """Provisioning is a detached background job; its only channel is the log."""
        opener = mock.Mock()
        opener.open = mock.Mock(side_effect=urllib.error.URLError("dns"))
        with mock.patch.object(engine_source.urllib.request, "build_opener", return_value=opener):
            ok, error = engine_source.download_archive(tmp_path / "e.tar.gz")
        assert ok is False and "download failed" in error

    def test_the_skip_env_refuses_before_touching_the_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A test run must never be able to reach the network."""
        monkeypatch.setenv(engine_source.SKIP_DOWNLOAD_ENV, "1")
        with mock.patch.object(engine_source.urllib.request, "build_opener") as opener:
            ok, error = engine_source.download_archive(tmp_path / "e.tar.gz")
        assert ok is False and engine_source.SKIP_DOWNLOAD_ENV in error
        assert not opener.called

    def test_the_ssl_context_is_installed_on_the_handler_not_the_open_call(self):
        """`OpenerDirector.open` has NO `context` parameter — passing one there
        raises TypeError on every real download while every mock passes. This
        pins the context onto an HTTPSHandler instead."""
        import inspect

        assert (
            "context"
            not in inspect.signature(engine_source.urllib.request.OpenerDirector.open).parameters
        )
        captured: list[object] = []

        def spy(*handlers):
            captured.extend(handlers)
            return mock.Mock(open=mock.Mock(side_effect=urllib.error.URLError("stop")))

        with mock.patch.object(engine_source.urllib.request, "build_opener", spy):
            engine_source.download_archive(Path("/tmp/kc-unused-engine.tar.gz"))
        https = [h for h in captured if isinstance(h, engine_source.urllib.request.HTTPSHandler)]
        assert https, "an HTTPSHandler carrying the SSL context must be installed"
        assert getattr(https[0], "_context", None) is not None

    def test_a_non_https_redirect_is_refused(self):
        """A hop to http:// would be a silent transport downgrade."""
        handler = engine_source._HttpsOnlyRedirectHandler()
        with pytest.raises(urllib.error.URLError):
            handler.redirect_request(
                mock.Mock(), mock.Mock(), 302, "Found", {}, "http://evil.example/e.tar.gz"
            )


class TestSafeExtraction:
    """`tarfile.extractall` writes wherever a member name points. Every one of
    these archives would escape the destination if it were extracted as given."""

    @pytest.mark.parametrize(
        "name",
        [
            "../escaped.txt",
            "a/../../escaped.txt",
            "/etc/cron.d/pwn",
            "..",
            "",
        ],
    )
    def test_traversal_and_absolute_names_are_refused(self, name: str, tmp_path: Path):
        info = tarfile.TarInfo(name or "x")
        info.name = name  # set directly: TarInfo("") is not constructible cleanly
        with pytest.raises(engine_source.ArchiveRejected):
            engine_source._tar_data_filter(info, str(tmp_path))

    def test_a_windows_style_absolute_name_is_refused(self, tmp_path: Path):
        """A backslash IS a separator on Windows, so it is refused outright."""
        info = tarfile.TarInfo("C:\\Windows\\System32\\evil.dll")
        with pytest.raises(engine_source.ArchiveRejected):
            engine_source._tar_data_filter(info, str(tmp_path))

    def test_a_nul_byte_in_a_name_is_refused(self, tmp_path: Path):
        info = tarfile.TarInfo("ok.txt\0/../../evil")
        with pytest.raises(engine_source.ArchiveRejected):
            engine_source._tar_data_filter(info, str(tmp_path))

    @pytest.mark.parametrize(
        "member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE]
    )
    def test_non_regular_members_are_refused(self, member_type: bytes, tmp_path: Path):
        """A symlink or hardlink can point out of the destination even when its
        OWN name looks innocent; a device/FIFO has no business in a source tree."""
        info = tarfile.TarInfo("innocent")
        info.type = member_type
        info.linkname = "/etc/passwd"
        with pytest.raises(engine_source.ArchiveRejected):
            engine_source._tar_data_filter(info, str(tmp_path))

    def test_an_oversized_member_is_refused(self, tmp_path: Path):
        info = tarfile.TarInfo("huge.bin")
        info.size = engine_source._MAX_MEMBER_BYTES + 1
        with pytest.raises(engine_source.ArchiveRejected):
            engine_source._tar_data_filter(info, str(tmp_path))

    def test_ownership_and_permission_bits_are_dropped(self, tmp_path: Path):
        """An upstream setuid or group-writable bit must not survive install."""
        info = tarfile.TarInfo("bin/thing")
        info.mode = 0o4777
        info.uid, info.gid = 1234, 5678
        scrubbed = engine_source._tar_data_filter(info, str(tmp_path))
        assert scrubbed.mode == engine_source._FILE_MODE
        assert scrubbed.uid == 0 and scrubbed.gid == 0

    def test_a_malicious_tar_writes_nothing_outside_the_destination(self, tmp_path: Path):
        """End to end through `_extract_tar`, which is what actually runs: the
        traversal member must be refused and the escape target must not exist."""
        archive = tmp_path / "evil.tar.gz"
        payload = b"pwned"
        evil = tarfile.TarInfo("../../escaped.txt")
        archive.write_bytes(_tar_bytes([evil], {"../../escaped.txt": payload}))
        dest = tmp_path / "unpack" / "deep"
        dest.mkdir(parents=True)
        with pytest.raises(engine_source.ArchiveRejected):
            engine_source._extract_tar(archive, dest)
        assert not (tmp_path / "escaped.txt").exists()
        assert not (tmp_path / "unpack" / "escaped.txt").exists()

    def test_a_symlink_escape_tar_is_refused_end_to_end(self, tmp_path: Path):
        """The member name is innocent; only the TYPE gives it away."""
        archive = tmp_path / "link.tar.gz"
        link = tarfile.TarInfo("passwd")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.write_bytes(_tar_bytes([link], {}))
        dest = tmp_path / "unpack"
        dest.mkdir()
        with pytest.raises(engine_source.ArchiveRejected):
            engine_source._extract_tar(archive, dest)
        assert not (dest / "passwd").exists()

    def test_an_archive_that_expands_too_far_is_refused(self, tmp_path: Path):
        """Bounds a decompression bomb even though the digest pin already means
        the archive can only be the one named."""
        archive = tmp_path / "bomb.tar.gz"
        archive.write_bytes(_benign_engine_tar())
        with mock.patch.object(engine_source, "_MAX_TOTAL_BYTES", 1):
            with pytest.raises(engine_source.ArchiveRejected):
                engine_source._extract_tar(archive, tmp_path / "unpack")

    def test_a_benign_archive_extracts(self, tmp_path: Path):
        """The guards must not reject the real thing."""
        archive = tmp_path / "ok.tar.gz"
        archive.write_bytes(_benign_engine_tar())
        dest = tmp_path / "unpack"
        dest.mkdir()
        engine_source._extract_tar(archive, dest)
        assert (dest / "repo-abc123" / "mcp-local" / "pyproject.toml").is_file()

    def test_the_python_310_leg_applies_the_same_filter(self, tmp_path: Path):
        """`filter=` does not exist on Python 3.10, which this project supports —
        the TypeError fallback must validate members, not skip the check."""
        archive = tmp_path / "evil.tar.gz"
        evil = tarfile.TarInfo("../../escaped.txt")
        archive.write_bytes(_tar_bytes([evil], {"../../escaped.txt": b"pwned"}))
        dest = tmp_path / "unpack"
        dest.mkdir()
        real_open = tarfile.open

        class _NoFilterTar:
            """A tarfile whose extractall rejects the `filter` kwarg, as 3.10's does."""

            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

            def getmembers(self):
                return self._inner.getmembers()

            def extractall(self, path, members=None, **kwargs):
                if "filter" in kwargs:
                    raise TypeError("extractall() got an unexpected keyword argument 'filter'")
                return self._inner.extractall(path, members=members)

        with mock.patch.object(
            engine_source.tarfile,
            "open",
            lambda *a, **kw: _NoFilterTar(real_open(*a, **kw).__enter__()),
        ):
            with pytest.raises(engine_source.ArchiveRejected):
                engine_source._extract_tar(archive, dest)
        assert not (tmp_path / "escaped.txt").exists()


class TestSourceMarker:
    """The marker replaces `(root / ".git").is_dir()` as the "is the vetted tree
    on disk?" probe — so nothing but a verified install may write it."""

    def test_a_written_marker_reads_back_as_installed(self, tmp_path: Path):
        engine_source.write_source_marker(tmp_path)
        assert engine_source.is_installed(tmp_path) is True
        assert engine_source.installed_tag(tmp_path) == engine_source.ENGINE_TAG

    def test_an_absent_marker_is_not_installed(self, tmp_path: Path):
        """A former git-based checkout has no marker, so it correctly reads as
        "needs re-fetching" rather than being trusted."""
        (tmp_path / ".git").mkdir()
        assert engine_source.is_installed(tmp_path) is False
        assert engine_source.installed_tag(tmp_path) == "unknown"

    def test_a_marker_from_a_different_commit_is_not_installed(self, tmp_path: Path):
        """Bumping the pin must make an existing install re-fetch, not silently
        keep an older engine."""
        (tmp_path / engine_source.SOURCE_MARKER_FILENAME).write_text(
            json.dumps(
                {
                    "tag": "v0.0.1",
                    "commit": "a" * 40,
                    "sha256": engine_source.ENGINE_TARBALL_SHA256,
                }
            ),
            encoding="utf-8",
        )
        assert engine_source.is_installed(tmp_path) is False

    def test_a_marker_from_a_different_digest_is_not_installed(self, tmp_path: Path):
        """Both halves are checked: the digest is the trust anchor, so a marker
        that names the right commit with the wrong bytes is not the vetted tree."""
        (tmp_path / engine_source.SOURCE_MARKER_FILENAME).write_text(
            json.dumps(
                {
                    "tag": engine_source.ENGINE_TAG,
                    "commit": engine_source.ENGINE_COMMIT,
                    "sha256": "b" * 64,
                }
            ),
            encoding="utf-8",
        )
        assert engine_source.is_installed(tmp_path) is False

    def test_a_corrupt_marker_is_not_installed(self, tmp_path: Path):
        (tmp_path / engine_source.SOURCE_MARKER_FILENAME).write_text("{not json", encoding="utf-8")
        assert engine_source.read_source_marker(tmp_path) == {}
        assert engine_source.is_installed(tmp_path) is False

    def test_a_marker_that_is_not_an_object_is_not_installed(self, tmp_path: Path):
        (tmp_path / engine_source.SOURCE_MARKER_FILENAME).write_text("[1, 2]", encoding="utf-8")
        assert engine_source.read_source_marker(tmp_path) == {}
        assert engine_source.is_installed(tmp_path) is False

    def test_the_reported_tag_never_outruns_verification(self, tmp_path: Path):
        """An honest tag: a tree whose digest was not checked reports "unknown"
        rather than the tag this code happens to be pinned to."""
        (tmp_path / engine_source.SOURCE_MARKER_FILENAME).write_text(
            json.dumps({"tag": "v9.9.9", "commit": "c" * 40, "sha256": "d" * 64}),
            encoding="utf-8",
        )
        assert engine_source.installed_tag(tmp_path) == "unknown"


class TestInstallEngine:
    """Fetch -> verify -> extract -> swap. A failure at any step must leave the
    PREVIOUS tree exactly as it was."""

    def _patch_fetch(self, data: bytes | None, *, error: str = "boom"):
        """Patch `download_archive` to write *data*, or to fail with *error*."""

        def fake(staging: Path) -> tuple[bool, str]:
            if data is None:
                return False, error
            staging.parent.mkdir(parents=True, exist_ok=True)
            staging.write_bytes(data)
            return True, ""

        return mock.patch.object(engine_source, "download_archive", side_effect=fake)

    def test_a_verified_archive_installs_and_marks_the_tree(self, tmp_path: Path):
        root = tmp_path / "vendor" / "sdpm"
        log: list[str] = []
        with self._patch_fetch(_benign_engine_tar()):
            assert engine_source.install_engine(root, log) is True
        assert (root / "mcp-local" / "pyproject.toml").is_file()
        assert engine_source.is_installed(root) is True
        assert any(engine_source.ENGINE_TAG in line for line in log)

    def test_the_marker_is_written_before_the_swap_not_after(self, tmp_path: Path):
        """A failing marker write must not be able to leave NO usable engine.

        The marker was previously written into the LIVE tree after ``_swap_in``,
        which had already retired the previous engine — so a failure there left a
        complete new tree that ``is_installed`` reports as absent, with the old one
        gone as well. Marking the staging tree makes the swap the last fallible
        step, and the swap is the step that already rolls back.

        Asserted by observing the ORDER rather than by faking a failure: the write
        must happen while the destination does not yet exist.
        """
        root = tmp_path / "vendor" / "sdpm"
        order: list[str] = []
        real_marker = engine_source.write_source_marker
        real_swap = engine_source._swap_in

        def spy_marker(path: Path) -> None:
            order.append(f"marker:{'live' if path == root else 'staging'}")
            real_marker(path)

        def spy_swap(src: Path, dst: Path, finalize=None) -> bool:  # noqa: ANN001
            order.append("swap")
            return real_swap(src, dst, finalize)

        with self._patch_fetch(_benign_engine_tar()), mock.patch.object(
            engine_source, "write_source_marker", side_effect=spy_marker
        ), mock.patch.object(engine_source, "_swap_in", side_effect=spy_swap):
            assert engine_source.install_engine(root, []) is True

        assert order == ["marker:staging", "swap"], (
            "the marker must be written into the staging tree BEFORE the swap; "
            f"got {order}"
        )
        # And the end state is still correct.
        assert engine_source.is_installed(root) is True

    def test_a_failed_build_keeps_the_previous_engine(self, tmp_path: Path):
        """Validation runs on the STAGED tree, before it replaces a working one.

        The venv lives inside the engine tree, so the swap discards the old one
        either way — and if the dependency resolve then failed, the user was left
        with no usable engine at all and the app simply unavailable. Validating
        first makes the worst case "still on the previous version", the same
        degradation a refused download already gives.
        """
        root = tmp_path / "vendor" / "sdpm"
        (root / "mcp-local").mkdir(parents=True)
        (root / "mcp-local" / "keep.txt").write_text("previous engine", encoding="utf-8")
        seen: list[Path] = []

        def failing_build(staged: Path) -> bool:
            seen.append(staged)
            return False

        log: list[str] = []
        with self._patch_fetch(_benign_engine_tar()):
            assert engine_source.install_engine(root, log, validate=failing_build) is False

        # Validation was handed the STAGING tree, not the live root.
        assert seen and seen[0] != root
        # And the previous engine is untouched.
        assert (root / "mcp-local" / "keep.txt").read_text(encoding="utf-8") == "previous engine"
        assert any("keeping the existing one" in line for line in log)

    def test_a_successful_build_swaps_the_new_tree_in(self, tmp_path: Path):
        """The other direction: validation passing must not block the install."""
        root = tmp_path / "vendor" / "sdpm"
        with self._patch_fetch(_benign_engine_tar()):
            assert engine_source.install_engine(root, [], validate=lambda _p: True) is True
        assert (root / "mcp-local" / "pyproject.toml").is_file()
        assert engine_source.is_installed(root) is True

    def test_the_wrapper_directory_is_unwrapped(self, tmp_path: Path):
        """A GitHub /archive/ tarball wraps everything in `<repo>-<sha>/`; the
        engine is that directory's CONTENTS, or `engine_root/mcp-local` breaks."""
        root = tmp_path / "vendor" / "sdpm"
        with self._patch_fetch(_benign_engine_tar()):
            engine_source.install_engine(root, [])
        assert not list(root.glob("repo-abc123"))
        assert (root / "mcp-local").is_dir()

    def test_an_already_pinned_tree_is_left_alone_with_no_network_call(self, tmp_path: Path):
        """Idempotence — re-provisioning must not re-download 3MB."""
        root = tmp_path / "vendor" / "sdpm"
        root.mkdir(parents=True)
        (root / "mcp-local").mkdir()
        engine_source.write_source_marker(root)
        log: list[str] = []
        with mock.patch.object(engine_source, "download_archive") as download:
            assert engine_source.install_engine(root, log) is True
        assert not download.called
        assert any("already at" in line for line in log)

    def test_a_refused_digest_keeps_the_previous_tree(self, tmp_path: Path):
        """Degrade to "still on the old version", never to "broken": a user with
        a working older engine must not lose it to a refused or failed fetch."""
        root = tmp_path / "vendor" / "sdpm"
        (root / "mcp-local").mkdir(parents=True)
        (root / "mcp-local" / "keep.txt").write_text("previous engine", encoding="utf-8")
        log: list[str] = []
        with self._patch_fetch(None, error="REFUSING the engine archive: sha256 got aaa…"):
            assert engine_source.install_engine(root, log) is False
        assert (root / "mcp-local" / "keep.txt").read_text(encoding="utf-8") == "previous engine"
        assert any("REFUSING" in line for line in log)

    def test_a_rejected_archive_keeps_the_previous_tree(self, tmp_path: Path):
        """Same guarantee for a hostile (rather than merely wrong) archive."""
        root = tmp_path / "vendor" / "sdpm"
        (root / "mcp-local").mkdir(parents=True)
        (root / "mcp-local" / "keep.txt").write_text("previous engine", encoding="utf-8")
        evil = tarfile.TarInfo("../../escaped.txt")
        log: list[str] = []
        with self._patch_fetch(_tar_bytes([evil], {"../../escaped.txt": b"pwned"})):
            assert engine_source.install_engine(root, log) is False
        assert (root / "mcp-local" / "keep.txt").is_file()
        assert any("rejected" in line for line in log)
        assert not (tmp_path / "escaped.txt").exists()

    def test_an_archive_without_mcp_local_is_refused(self, tmp_path: Path):
        """Guards against installing a tree that is not the engine at all."""
        wrong = io.BytesIO()
        with tarfile.open(fileobj=wrong, mode="w:gz") as tar:
            d = tarfile.TarInfo("repo-abc123")
            d.type = tarfile.DIRTYPE
            tar.addfile(d)
        root = tmp_path / "vendor" / "sdpm"
        log: list[str] = []
        with self._patch_fetch(wrong.getvalue()):
            assert engine_source.install_engine(root, log) is False
        assert any("mcp-local" in line for line in log)
        assert engine_source.is_installed(root) is False

    def test_a_multi_root_archive_is_refused(self, tmp_path: Path):
        """A GitHub /archive/ tarball has exactly one top-level dir; anything
        else is not the artifact this module pinned."""
        odd = io.BytesIO()
        with tarfile.open(fileobj=odd, mode="w:gz") as tar:
            for name in ("one", "two"):
                d = tarfile.TarInfo(name)
                d.type = tarfile.DIRTYPE
                tar.addfile(d)
        root = tmp_path / "vendor" / "sdpm"
        log: list[str] = []
        with self._patch_fetch(odd.getvalue()):
            assert engine_source.install_engine(root, log) is False
        assert any("single source tree" in line for line in log)

    def test_an_upgrade_replaces_the_old_tree_entirely(self, tmp_path: Path):
        """Stale files from an older engine must not survive into the new tree —
        the engine resolves its own bundled data relative to the checkout."""
        root = tmp_path / "vendor" / "sdpm"
        (root / "mcp-local").mkdir(parents=True)
        (root / "stale-from-v0.3.7.txt").write_text("old", encoding="utf-8")
        (root / engine_source.SOURCE_MARKER_FILENAME).write_text(
            json.dumps({"tag": "v0.3.7", "commit": "e" * 40, "sha256": "f" * 64}),
            encoding="utf-8",
        )
        log: list[str] = []
        with self._patch_fetch(_benign_engine_tar()):
            assert engine_source.install_engine(root, log) is True
        assert not (root / "stale-from-v0.3.7.txt").exists()
        assert engine_source.is_installed(root) is True
        assert any("updating the engine" in line for line in log)

    def test_the_scratch_directory_is_always_removed(self, tmp_path: Path):
        """A failed provision must not leave a 3MB archive and an unpacked tree
        behind, or repeated retries fill the data dir."""
        root = tmp_path / "vendor" / "sdpm"
        with self._patch_fetch(None):
            engine_source.install_engine(root, [])
        leftovers = list((root.parent).glob(".engine-fetch.*")) if root.parent.exists() else []
        assert leftovers == []

    def test_the_marker_is_written_only_after_a_successful_extraction(self, tmp_path: Path):
        """The marker is what `is_installed` trusts, so an install that failed
        mid-way must not have written it."""
        root = tmp_path / "vendor" / "sdpm"
        with (
            self._patch_fetch(_benign_engine_tar()),
            mock.patch.object(engine_source, "_swap_in", side_effect=OSError("disk full")),
        ):
            assert engine_source.install_engine(root, []) is False
        assert engine_source.is_installed(root) is False


class TestSwapRollback:
    """A failed swap must never leave the user with NO engine.

    `_swap_in` moves the old tree aside before renaming the new one into place. If
    that second rename fails, the old tree is the only copy left — an unconditional
    cleanup deleted it too, turning a failed *update* into a broken install. The
    contract is the same one `_ensure_clone` already promises: the worst case is
    "still on the previous version".
    """

    @staticmethod
    def _trees(tmp_path: Path) -> tuple[Path, Path]:
        engine = tmp_path / "sdpm"
        engine.mkdir()
        (engine / "WORKING").write_text("v1", encoding="utf-8")
        extracted = tmp_path / "unpacked"
        extracted.mkdir()
        (extracted / "NEW").write_text("v2", encoding="utf-8")
        return engine, extracted

    def test_a_failed_swap_restores_the_previous_engine(self, tmp_path: Path) -> None:
        engine, extracted = self._trees(tmp_path)
        real = os.replace
        calls: list[int] = []

        def flaky(src, dst):  # noqa: ANN001 - matches os.replace
            calls.append(1)
            # Fail the SECOND replace: staging -> engine_root, with the old tree
            # already moved aside. That is the only window where both can be lost.
            if len(calls) == 2:
                raise OSError("simulated swap failure")
            return real(src, dst)

        with mock.patch.object(engine_source.os, "replace", flaky):
            with pytest.raises(OSError):
                engine_source._swap_in(extracted, engine)

        assert engine.is_dir()
        assert (engine / "WORKING").read_text(encoding="utf-8") == "v1"
        # No `.old`/`.new` debris left behind.
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    def test_a_successful_swap_installs_the_new_tree(self, tmp_path: Path) -> None:
        """The happy path must survive the rollback branch."""
        engine, extracted = self._trees(tmp_path)
        assert engine_source._swap_in(extracted, engine) is True
        assert (engine / "NEW").is_file()
        assert not (engine / "WORKING").exists()
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    def test_a_failed_finalize_restores_the_previous_engine(self, tmp_path: Path) -> None:
        """`finalize` runs at the FINAL path, so its failure must roll back too.

        This is the window the retired tree exists for. The editable re-link has to run
        after the rename (an `--editable` install records an absolute path, so linking
        in staging records a directory the swap then deletes) — but the swap removed
        the retired tree as soon as the new one landed, so relinking AFTER
        `install_engine` returned had no rollback state left. A failure there deleted
        the user's working engine and left the new one pointing at a removed path:
        no usable engine at all, which is precisely what the staged validation exists
        to prevent, reached one step later.
        """
        engine, extracted = self._trees(tmp_path)
        seen: list[Path] = []

        def failing_finalize(final: Path) -> bool:
            # It must be handed the FINAL path — being given staging is what produced
            # the stale `.pth` in the first place.
            seen.append(final)
            assert (final / "NEW").is_file(), "finalize ran before the swap completed"
            return False

        assert engine_source._swap_in(extracted, engine, failing_finalize) is False

        assert seen == [engine]
        # The user's engine is back, unchanged.
        assert engine.is_dir()
        assert (engine / "WORKING").read_text(encoding="utf-8") == "v1"
        assert not (engine / "NEW").exists()
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    @pytest.mark.parametrize(
        "exc", [OSError("disk full"), RuntimeError("boom")], ids=["oserror", "runtimeerror"]
    )
    def test_a_RAISING_finalize_also_restores_the_previous_engine(
        self, tmp_path: Path, exc: BaseException
    ) -> None:
        """Raising and returning False are the same outcome, so they take the same unwind.

        Treating a raise as "not a failure" was silent DATA LOSS. The exception escaped
        the finalize branch; the `except OSError` handler could not help because it
        restores only when `engine_root` does not exist — and the new tree is sitting
        there — so the `finally` saw a populated root and deleted the retired tree,
        taking the user's WORKING engine with it. Verified against the pre-fix code:
        `engine_root` held only `NEW` and `WORKING` was gone.

        Both exception types are covered because `OSError` was not even nominally
        handled here, and a `RuntimeError` from a sandbox-setup step is just as likely.
        """
        engine, extracted = self._trees(tmp_path)

        def raising_finalize(final: Path) -> bool:
            raise exc

        with pytest.raises(type(exc)):
            engine_source._swap_in(extracted, engine, raising_finalize)

        # The exception propagates (nothing is swallowed) AND the engine survives.
        assert engine.is_dir()
        assert (engine / "WORKING").read_text(encoding="utf-8") == "v1"
        assert not (engine / "NEW").exists()
        # No staging/retired debris left behind.
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    def test_a_raising_finalize_on_a_FIRST_install_leaves_nothing_marked(
        self, tmp_path: Path
    ) -> None:
        """Same as the returns-False first-install case: with nothing to restore, the
        new tree must still be REMOVED so no source marker survives and the next
        attempt re-fetches instead of short-circuiting on `is_installed()`."""
        engine = tmp_path / "engine"
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        (extracted / "NEW").write_text("new", encoding="utf-8")

        def raising_finalize(final: Path) -> bool:
            raise OSError("disk full")

        with pytest.raises(OSError):
            engine_source._swap_in(extracted, engine, raising_finalize)

        assert not engine.exists()
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    def test_a_failed_finalize_on_a_FIRST_install_leaves_nothing_marked(
        self, tmp_path: Path
    ) -> None:
        """With no previous engine there is nothing to restore — but the new tree must
        still be REMOVED, not left in place.

        This is the case a `moved_aside` guard got wrong. Leaving the unusable tree at
        `engine_root` left its source marker there too, and `install_engine`
        short-circuits on `is_installed()` — so a first install whose finalize failed
        reported failure once and then read as ALREADY INSTALLED forever. Every retry
        returned early, no repair ran, and `sdpm` never imported: a permanent broken
        state reachable from one transient failure.
        """
        extracted = tmp_path / "unpacked"
        extracted.mkdir()
        (extracted / "NEW").write_text("v1", encoding="utf-8")
        # The marker `install_engine` writes into the staging tree before the swap; it
        # is what makes the leftover read as installed.
        engine_source.write_source_marker(extracted)
        engine = tmp_path / "sdpm"

        assert engine_source._swap_in(extracted, engine, lambda final: False) is False

        assert not engine.exists(), "an unusable first install was left at engine_root"
        assert not engine_source.is_installed(engine), (
            "the leftover marker makes install_engine short-circuit, so no retry repairs it"
        )
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    def test_a_successful_finalize_keeps_the_new_tree(self, tmp_path: Path) -> None:
        """The rollback must not fire on success — a fix that always restored the old
        tree would satisfy the test above."""
        engine, extracted = self._trees(tmp_path)
        assert engine_source._swap_in(extracted, engine, lambda final: True) is True
        assert (engine / "NEW").is_file()
        assert not (engine / "WORKING").exists()
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    def test_a_first_install_needs_no_rollback(self, tmp_path: Path) -> None:
        """With no existing engine there is nothing to move aside."""
        extracted = tmp_path / "unpacked"
        extracted.mkdir()
        (extracted / "NEW").write_text("v1", encoding="utf-8")
        engine = tmp_path / "sdpm"
        engine_source._swap_in(extracted, engine)
        assert (engine / "NEW").is_file()
