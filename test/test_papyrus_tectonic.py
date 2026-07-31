"""Tests for Papyrus's managed, digest-pinned Tectonic install (``tectonic.py``).

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

**No test here reaches the network.** Every download is mocked at the
``urllib.request`` opener, and :data:`tectonic.SKIP_DOWNLOAD_ENV` is set for the
whole module as a second belt: even a test that slipped past the mock would be
refused before a socket was opened, rather than pulling 22MB in CI.

Coverage targets:

  * the platform → asset mapping, including the arm64/aarch64 and AMD64/x86_64
    naming splits, and that an unsupported host degrades rather than raising;
  * **digest mismatch refusal** — a payload whose sha256 does not match the pin is
    discarded and nothing is installed;
  * **archive-traversal refusal** — a malicious ``.tar.gz`` or ``.zip`` (``..``
    members, absolute paths, symlinks) is refused before anything is written,
    which is the whole reason a bare ``extractall`` is not used;
  * the happy path: verify → extract → atomic install → executable bit;
  * that a user's own ``pdflatex`` on ``PATH`` still WINS over the managed install
    (the managed copy must never displace a real TeX distribution);
  * that the resolution cache is reset after an install, or a stale "no compiler"
    answer sticks;
  * that no download runs on the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Iterator
from unittest import mock

import pytest

from kiro_crew.apps.builtins.papyrus.backend import latex, store, tectonic


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Belt-and-braces: a test run must never fetch the real 22MB artifact."""
    monkeypatch.setenv(tectonic.SKIP_DOWNLOAD_ENV, "1")
    monkeypatch.delenv(tectonic.TECTONIC_URL_ENV, raising=False)
    tectonic.reset_provision_state()
    latex.reset_compiler_cache()
    yield
    tectonic.reset_provision_state()
    latex.reset_compiler_cache()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at a tmp data dir so no test touches the real app home."""
    root = tmp_path / "papyrus-data"
    root.mkdir()
    monkeypatch.setattr(store, "app_data_dir", lambda _name: root)
    return root


def _payload(size: int = tectonic._MIN_BINARY_BYTES + 1) -> bytes:
    """A body large enough to pass the plausibility floor, cheap to build."""
    return b"\x7fELF" + b"\0" * (size - 4)


def _tar_gz(members: dict[str, bytes]) -> bytes:
    """A ``.tar.gz`` of regular-file members."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _tar_gz_special(name: str, kind: str, linkname: str = "") -> bytes:
    """A ``.tar.gz`` holding ONE non-regular member (symlink/link/fifo/dev)."""
    buf = io.BytesIO()
    types = {
        "sym": tarfile.SYMTYPE,
        "link": tarfile.LNKTYPE,
        "fifo": tarfile.FIFOTYPE,
        "chr": tarfile.CHRTYPE,
    }
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.type = types[kind]
        info.linkname = linkname
        tar.addfile(info)
    return buf.getvalue()


def _zip_of(members: dict[str, bytes]) -> bytes:
    """A ``.zip`` whose member names are stored EXACTLY as given.

    The name is assigned after ``ZipInfo.__init__`` on purpose. That constructor
    rewrites ``\\`` to ``/`` whenever ``os.sep`` is ``\\``, so a plain
    ``writestr("a\\b", …)`` silently becomes ``a/b`` on Windows — the archive
    under test would no longer contain the member the test means to plant, and a
    backslash-traversal test would pass by not testing anything.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            info = zipfile.ZipInfo("placeholder")
            info.filename = name
            zf.writestr(info, body)
    return buf.getvalue()


class _FakeResponse(io.BytesIO):
    """Minimal stand-in for a urllib response: readable + ``.headers``."""

    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _serve(body: bytes) -> mock._patch:
    """Patch the module's opener so a download yields *body* and touches no socket."""
    opener = mock.MagicMock()
    opener.open.return_value = _FakeResponse(body)
    return mock.patch.object(tectonic.urllib.request, "build_opener", return_value=opener)


def _pin(monkeypatch: pytest.MonkeyPatch, asset: tectonic.TectonicAsset) -> None:
    """Make :func:`tectonic.current_asset` resolve to *asset* on any host."""
    monkeypatch.setattr(tectonic, "current_asset", lambda: asset)


def _asset_for(body: bytes, *, name: str) -> tectonic.TectonicAsset:
    """An asset whose pin is the REAL digest of *body*."""
    return tectonic.TectonicAsset(name, hashlib.sha256(body).hexdigest())


def _allow_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the skip-download guard for a test that mocks the transport itself."""
    monkeypatch.delenv(tectonic.SKIP_DOWNLOAD_ENV, raising=False)


# ── platform → asset mapping ────────────────────────────────────────────────


class TestPlatformMapping:
    @pytest.mark.parametrize(
        ("machine", "expected"),
        [
            ("x86_64", tectonic._ARCH_X86_64),
            ("X86_64", tectonic._ARCH_X86_64),
            ("AMD64", tectonic._ARCH_X86_64),  # Windows spells it this way
            ("amd64", tectonic._ARCH_X86_64),
            ("x64", tectonic._ARCH_X86_64),
            ("aarch64", tectonic._ARCH_AARCH64),  # Linux
            ("arm64", tectonic._ARCH_AARCH64),  # macOS
            ("ARM64", tectonic._ARCH_AARCH64),
        ],
    )
    def test_normalizes_every_arch_spelling(self, machine: str, expected: str) -> None:
        assert tectonic._arch_key(machine) == expected

    @pytest.mark.parametrize("machine", ["i686", "armv7l", "ppc64le", "s390x", "", "riscv64"])
    def test_an_unknown_arch_is_none(self, machine: str) -> None:
        assert tectonic._arch_key(machine) is None

    @pytest.mark.parametrize(
        ("platform_name", "expected"),
        [
            ("linux", tectonic._PLATFORM_LINUX),
            ("linux2", tectonic._PLATFORM_LINUX),
            ("darwin", tectonic._PLATFORM_DARWIN),
            ("win32", tectonic._PLATFORM_WINDOWS),
        ],
    )
    def test_normalizes_platform_names(self, platform_name: str, expected: str) -> None:
        assert tectonic._platform_key(platform_name) == expected

    @pytest.mark.parametrize("platform_name", ["freebsd13", "cygwin", "aix", "sunos5"])
    def test_an_unsupported_platform_is_none(self, platform_name: str) -> None:
        assert tectonic._platform_key(platform_name) is None

    @pytest.mark.parametrize(
        ("platform_name", "machine", "fragment"),
        [
            ("darwin", "arm64", "aarch64-apple-darwin"),
            ("darwin", "x86_64", "x86_64-apple-darwin"),
            ("linux", "x86_64", "x86_64-unknown-linux-musl"),
            ("linux", "aarch64", "aarch64-unknown-linux-musl"),
            ("win32", "AMD64", "x86_64-pc-windows-msvc"),
        ],
    )
    def test_resolves_the_expected_asset(
        self, monkeypatch: pytest.MonkeyPatch, platform_name: str, machine: str, fragment: str
    ) -> None:
        monkeypatch.setattr(tectonic.sys, "platform", platform_name)
        monkeypatch.setattr(tectonic.platform, "machine", lambda: machine)
        asset = tectonic.current_asset()
        assert asset is not None
        assert fragment in asset.name
        assert tectonic.platform_supported() is True

    def test_linux_uses_the_static_musl_builds(self) -> None:
        """A glibc build would carry a version floor that breaks older LTS images."""
        for arch in (tectonic._ARCH_X86_64, tectonic._ARCH_AARCH64):
            asset = tectonic._ASSETS[(tectonic._PLATFORM_LINUX, arch)]
            assert "musl" in asset.name
            assert "gnu" not in asset.name

    @pytest.mark.parametrize(
        ("platform_name", "machine"),
        [
            ("linux", "i686"),  # 32-bit: no pinned build
            ("freebsd13", "x86_64"),  # not a supported platform
            ("win32", "ARM64"),  # the release publishes no aarch64 Windows asset
        ],
    )
    def test_an_unsupported_host_reports_rather_than_raises(
        self, monkeypatch: pytest.MonkeyPatch, platform_name: str, machine: str
    ) -> None:
        monkeypatch.setattr(tectonic.sys, "platform", platform_name)
        monkeypatch.setattr(tectonic.platform, "machine", lambda: machine)
        assert tectonic.current_asset() is None
        assert tectonic.platform_supported() is False


class TestPins:
    def test_every_pin_is_a_real_sha256(self) -> None:
        """A placeholder or truncated digest would silently accept any payload."""
        for key, asset in tectonic._ASSETS.items():
            assert len(asset.sha256) == 64, key
            assert set(asset.sha256) <= set("0123456789abcdef"), key
            # A digest of all-one-character is the shape an invented pin takes.
            assert len(set(asset.sha256)) > 4, key

    def test_pins_are_distinct_per_platform(self) -> None:
        digests = [a.sha256 for a in tectonic._ASSETS.values()]
        assert len(digests) == len(set(digests))

    def test_every_asset_url_is_https_and_names_the_pinned_tag(self) -> None:
        for asset in tectonic._ASSETS.values():
            assert asset.url.startswith("https://")
            # The tag carries an '@', which must be percent-encoded in the path.
            assert "tectonic%400.17.0" in asset.url
            assert asset.name in asset.url

    def test_the_version_constant_matches_the_release_tag(self) -> None:
        assert tectonic.TECTONIC_RELEASE_TAG.endswith(tectonic.TECTONIC_VERSION)

    def test_only_windows_ships_a_zip(self) -> None:
        for (platform_name, _arch), asset in tectonic._ASSETS.items():
            assert asset.is_zip is (platform_name == tectonic._PLATFORM_WINDOWS)


# ── install paths ───────────────────────────────────────────────────────────


class TestInstallPaths:
    def test_installs_inside_the_apps_own_data_dir(self, data_root: Path) -> None:
        """Never a system prefix — that is what separates this from a package install."""
        target = tectonic.binary_path()
        assert data_root in target.parents
        assert target.parent.name == "tectonic"
        assert target.parent.parent.name == "vendor"

    def test_is_not_installed_on_a_fresh_data_dir(self, data_root: Path) -> None:
        assert tectonic.binary_installed() is False

    def test_a_truncated_binary_does_not_read_as_installed(self, data_root: Path) -> None:
        """A half-written file must never look like a usable compiler."""
        target = tectonic.binary_path()
        target.parent.mkdir(parents=True)
        target.write_bytes(b"#!/bin/sh\n")
        target.chmod(0o755)
        assert tectonic.binary_installed() is False

    def test_a_plausible_executable_reads_as_installed(self, data_root: Path) -> None:
        target = tectonic.binary_path()
        target.parent.mkdir(parents=True)
        target.write_bytes(_payload())
        target.chmod(0o755)
        assert tectonic.binary_installed() is True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX exec bit")
    def test_a_non_executable_binary_does_not_read_as_installed(self, data_root: Path) -> None:
        target = tectonic.binary_path()
        target.parent.mkdir(parents=True)
        target.write_bytes(_payload())
        target.chmod(0o644)
        assert tectonic.binary_installed() is False


# ── URL resolution ──────────────────────────────────────────────────────────


class TestUrlResolution:
    def test_defaults_to_the_pinned_release_url(self) -> None:
        asset = tectonic._ASSETS[(tectonic._PLATFORM_LINUX, tectonic._ARCH_X86_64)]
        assert tectonic.resolve_url(asset) == asset.url

    def test_an_https_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asset = tectonic._ASSETS[(tectonic._PLATFORM_LINUX, tectonic._ARCH_X86_64)]
        monkeypatch.setenv(tectonic.TECTONIC_URL_ENV, "https://mirror.example/tectonic.tar.gz")
        assert tectonic.resolve_url(asset) == "https://mirror.example/tectonic.tar.gz"

    @pytest.mark.parametrize(
        "bad",
        [
            "http://mirror.example/tectonic.tar.gz",
            "file:///etc/passwd",
            "ftp://mirror.example/x",
            "/etc/passwd",
        ],
    )
    def test_a_non_https_override_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        """A plaintext or file:// override would read local files or downgrade transport."""
        asset = tectonic._ASSETS[(tectonic._PLATFORM_LINUX, tectonic._ARCH_X86_64)]
        monkeypatch.setenv(tectonic.TECTONIC_URL_ENV, bad)
        assert tectonic.resolve_url(asset) == asset.url

    def test_redacts_userinfo_and_query_from_a_logged_url(self) -> None:
        redacted = tectonic.redact_url("https://user:secret@mirror.example/t.tar.gz?sig=abc123")
        assert "secret" not in redacted
        assert "abc123" not in redacted
        assert redacted == "https://mirror.example/t.tar.gz"


# ── safe extraction ─────────────────────────────────────────────────────────


class TestMemberNameRules:
    @pytest.mark.parametrize(
        "name",
        [
            "../escape",
            "../../etc/cron.d/pwn",
            "a/../../b",
            "/etc/passwd",
            "/absolute",
            "C:/Windows/System32/x",
            "..\\windows",
            "dir\\file",
            "with\0nul",
            "",
        ],
    )
    def test_rejects_an_unsafe_member_name(self, name: str) -> None:
        assert tectonic._reject_member_name(name) is not None

    @pytest.mark.parametrize("name", ["tectonic", "tectonic.exe", "bin/tectonic", "a/b/c.txt"])
    def test_accepts_a_plain_relative_name(self, name: str) -> None:
        assert tectonic._reject_member_name(name) is None

    @pytest.mark.parametrize("name", ["./tectonic", "./bin/tectonic", "a/./b"])
    def test_accepts_a_dot_prefixed_name(self, name: str) -> None:
        """A leading ``./`` is a standard tar convention and a path no-op — refusing
        it would break a future release for no security gain. Only ``..`` escapes."""
        assert tectonic._reject_member_name(name) is None

    def test_rejects_dotdot_in_the_final_component_too(self) -> None:
        """A trailing ``..`` still resolves to the parent, so position is irrelevant."""
        assert tectonic._reject_member_name("bin/..") is not None


class TestSafeTarExtraction:
    def test_extracts_a_benign_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "ok.tar.gz"
        archive.write_bytes(_tar_gz({"tectonic": _payload(1024)}))
        dest = tmp_path / "out"
        dest.mkdir()
        tectonic._extract_tar(archive, dest)
        assert (dest / "tectonic").is_file()

    @pytest.mark.parametrize(
        "name", ["../escape", "../../../../../../tmp/pwn", "/etc/cron.d/pwn", "a/../../b"]
    )
    def test_refuses_a_traversal_member(self, tmp_path: Path, name: str) -> None:
        """THE reason a bare ``extractall`` is not used: a ``..`` or absolute member
        writes wherever it points, outside the destination entirely."""
        archive = tmp_path / "evil.tar.gz"
        archive.write_bytes(_tar_gz({name: b"pwned"}))
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(tectonic.ArchiveRejected):
            tectonic._extract_tar(archive, dest)
        # Nothing escaped, and nothing landed inside either.
        assert list(dest.iterdir()) == []
        assert not (tmp_path / "escape").exists()
        assert not (tmp_path / "pwn").exists()

    @pytest.mark.parametrize(
        ("kind", "linkname"),
        [
            ("sym", "/etc/passwd"),
            ("sym", "../../../../etc/passwd"),
            ("link", "/etc/shadow"),
            ("fifo", ""),
            ("chr", ""),
        ],
    )
    def test_refuses_a_non_regular_member(
        self, tmp_path: Path, kind: str, linkname: str
    ) -> None:
        """A symlink/hardlink member escapes even when its own NAME looks innocent,
        and a device/FIFO member has no business in a compiler tarball."""
        archive = tmp_path / "evil.tar.gz"
        archive.write_bytes(_tar_gz_special("tectonic", kind, linkname))
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(tectonic.ArchiveRejected):
            tectonic._extract_tar(archive, dest)
        assert list(dest.iterdir()) == []

    def test_refuses_an_oversized_member(self) -> None:
        """A decompression bomb is bounded even though the digest pin already means
        the archive can only be the artifact we named.

        Driven at the filter rather than through a real archive: writing a
        256MB+ member to disk to prove a header check would make the suite
        enormous, and the header-declared size IS what the guard reads.
        """
        info = tarfile.TarInfo("tectonic")
        info.size = tectonic._MAX_MEMBER_BYTES + 1
        with pytest.raises(tectonic.ArchiveRejected, match="too large"):
            tectonic._tar_data_filter(info, "/dest")

    def test_drops_ownership_and_normalizes_the_mode(self) -> None:
        info = tarfile.TarInfo("tectonic")
        info.uid, info.gid = 1234, 5678
        info.uname, info.gname = "root", "wheel"
        info.mode = 0o4777  # setuid
        info.size = 10
        scrubbed = tectonic._tar_data_filter(info, "/dest")
        assert scrubbed.uid == 0
        assert scrubbed.gid == 0
        assert scrubbed.uname == ""
        assert scrubbed.mode == tectonic._BINARY_MODE
        assert not scrubbed.mode & 0o4000, "setuid must never survive extraction"

    def test_the_python310_fallback_path_also_refuses_traversal(self, tmp_path: Path) -> None:
        """``filter="data"`` does not exist on Python 3.10, which this project still
        supports, so the manual member check must refuse the same archives."""
        archive = tmp_path / "evil.tar.gz"
        archive.write_bytes(_tar_gz({"../escape": b"pwned"}))
        dest = tmp_path / "out"
        dest.mkdir()

        real_open = tarfile.open

        class _NoFilterTar:
            """A tarfile whose ``extractall`` rejects the ``filter`` kwarg, as 3.10's does."""

            def __init__(self, inner: tarfile.TarFile) -> None:
                self._inner = inner

            def __enter__(self) -> "_NoFilterTar":
                return self

            def __exit__(self, *exc: object) -> None:
                self._inner.close()

            def getmembers(self) -> list[tarfile.TarInfo]:
                return self._inner.getmembers()

            def extractall(self, *args: object, **kwargs: object) -> None:
                if "filter" in kwargs:
                    raise TypeError("extractall() got an unexpected keyword argument 'filter'")
                self._inner.extractall(*args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(
            tectonic.tarfile, "open", lambda *a, **k: _NoFilterTar(real_open(*a, **k))
        ):
            with pytest.raises(tectonic.ArchiveRejected):
                tectonic._extract_tar(archive, dest)
        assert not (tmp_path / "escape").exists()


class TestSafeZipExtraction:
    def test_extracts_a_benign_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "ok.zip"
        archive.write_bytes(_zip_of({"tectonic.exe": _payload(1024)}))
        dest = tmp_path / "out"
        dest.mkdir()
        tectonic._extract_zip(archive, dest)
        assert (dest / "tectonic.exe").is_file()

    @pytest.mark.parametrize(
        "name", ["../escape", "../../../../tmp/pwn", "/etc/pwn", "C:/Windows/pwn", "a\\b"]
    )
    def test_refuses_a_traversal_member(self, tmp_path: Path, name: str) -> None:
        """``ZipFile`` has no ``filter`` hook, so the check is explicit — and it runs
        before ANY member is written, so a hostile archive lands nothing at all."""
        archive = tmp_path / "evil.zip"
        archive.write_bytes(_zip_of({name: b"pwned", "tectonic.exe": _payload(1024)}))
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(tectonic.ArchiveRejected):
            tectonic._extract_zip(archive, dest)
        assert list(dest.iterdir()) == [], "no member may be written when one is refused"
        assert not (tmp_path / "escape").exists()

    @pytest.mark.parametrize("name", ["a\\b", "..\\..\\evil", "C:\\Windows\\pwn"])
    def test_refuses_a_backslash_member_by_its_stored_name(
        self, tmp_path: Path, name: str
    ) -> None:
        """A backslash member is refused on its OWN spelling, not via a stdlib rewrite.

        `ZipInfo.__init__` rewrites `\\` to `/` when `os.sep` is `\\`, so on
        Windows `info.filename` never carries a backslash — the guard's backslash
        rule can only fire on POSIX, the one platform where a backslash is NOT a
        separator. That made the traversal check quietly dependent on a
        normalization detail. `_extract_zip` therefore validates
        `orig_filename` too; this pins that, checking the stored name directly so
        the assertion holds identically on POSIX and Windows.
        """
        archive = tmp_path / "evil.zip"
        archive.write_bytes(_zip_of({name: b"pwned", "tectonic.exe": _payload(1024)}))
        with zipfile.ZipFile(archive) as zf:
            stored = [i.orig_filename for i in zf.infolist()]
        assert name in stored, "the fixture must plant the backslash name verbatim"
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(tectonic.ArchiveRejected):
            tectonic._extract_zip(archive, dest)
        assert list(dest.iterdir()) == [], "no member may be written when one is refused"

    def test_refuses_a_unix_symlink_smuggled_in_external_attrs(self, tmp_path: Path) -> None:
        """Zip has no portable symlink type, but a Unix mode in the external
        attributes is how one is carried — and it escapes just as well."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo("tectonic")
            info.external_attr = (0o120777 << 16)  # S_IFLNK
            zf.writestr(info, "/etc/passwd")
        archive = tmp_path / "evil.zip"
        archive.write_bytes(buf.getvalue())
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(tectonic.ArchiveRejected):
            tectonic._extract_zip(archive, dest)
        assert list(dest.iterdir()) == []

    def test_refuses_an_oversized_member(self, tmp_path: Path) -> None:
        """``ZipFile.writestr`` recomputes ``file_size`` from the payload, so a
        declared-huge member cannot be built by writing one — the crafted
        ``infolist`` is what a zip bomb's central directory actually looks like."""
        archive = tmp_path / "bomb.zip"
        archive.write_bytes(_zip_of({"tectonic.exe": b"x"}))
        dest = tmp_path / "out"
        dest.mkdir()
        oversized = zipfile.ZipInfo("tectonic.exe")
        oversized.file_size = tectonic._MAX_MEMBER_BYTES + 1
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.infolist.return_value = [oversized]
        with mock.patch.object(tectonic.zipfile, "ZipFile", return_value=fake):
            with pytest.raises(tectonic.ArchiveRejected, match="too large"):
                tectonic._extract_zip(archive, dest)
        assert fake.extract.call_count == 0


# ── provisioning ────────────────────────────────────────────────────────────


class TestProvisionSync:
    def test_installs_a_verified_archive(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow_download(monkeypatch)
        body = _tar_gz({"tectonic": _payload()})
        archive = _tar_gz_bytes = body
        asset = _asset_for(_tar_gz_bytes, name="tectonic-test.tar.gz")
        _pin(monkeypatch, asset)
        monkeypatch.setattr(tectonic, "binary_name", lambda: "tectonic")
        with _serve(archive):
            assert tectonic.provision_sync(attempts=1) is True
        target = tectonic.binary_path()
        assert target.is_file()
        assert tectonic.binary_installed() is True
        assert tectonic.provision_state()["state"] == tectonic.STATE_DONE

    @pytest.mark.skipif(os.name == "nt", reason="POSIX exec bit")
    def test_the_installed_binary_is_executable_and_not_group_writable(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow_download(monkeypatch)
        body = _tar_gz({"tectonic": _payload()})
        _pin(monkeypatch, _asset_for(body, name="t.tar.gz"))
        monkeypatch.setattr(tectonic, "binary_name", lambda: "tectonic")
        with _serve(body):
            assert tectonic.provision_sync(attempts=1) is True
        mode = tectonic.binary_path().stat().st_mode & 0o777
        assert mode == tectonic._BINARY_MODE
        assert os.access(tectonic.binary_path(), os.X_OK)

    def test_a_digest_mismatch_refuses_the_download(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pin is the trust anchor: a payload that does not match it installs
        NOTHING, and the error names the mismatch."""
        _allow_download(monkeypatch)
        body = _tar_gz({"tectonic": _payload()})
        wrong = tectonic.TectonicAsset("t.tar.gz", "0" * 63 + "1")
        _pin(monkeypatch, wrong)
        monkeypatch.setattr(tectonic, "binary_name", lambda: "tectonic")
        with _serve(body):
            assert tectonic.provision_sync(attempts=1) is False
        assert not tectonic.binary_path().exists()
        assert tectonic.binary_installed() is False
        state = tectonic.provision_state()
        assert state["state"] == tectonic.STATE_ERROR
        assert "sha256 mismatch" in str(state["error"])

    def test_a_tampered_byte_is_caught(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins the real property: the digest is over the STREAM, so one flipped
        byte anywhere in a 22MB transfer is refused."""
        _allow_download(monkeypatch)
        body = bytearray(_tar_gz({"tectonic": _payload()}))
        asset = _asset_for(bytes(body), name="t.tar.gz")
        body[-1] ^= 0xFF
        _pin(monkeypatch, asset)
        monkeypatch.setattr(tectonic, "binary_name", lambda: "tectonic")
        with _serve(bytes(body)):
            assert tectonic.provision_sync(attempts=1) is False
        assert not tectonic.binary_path().exists()

    def test_a_traversal_archive_installs_nothing(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even a correctly-pinned archive is refused if its members are unsafe —
        the digest check and the extraction check are independent gates."""
        _allow_download(monkeypatch)
        body = _tar_gz({"../../escape": b"pwned"})
        _pin(monkeypatch, _asset_for(body, name="t.tar.gz"))
        monkeypatch.setattr(tectonic, "binary_name", lambda: "tectonic")
        with _serve(body):
            assert tectonic.provision_sync(attempts=1) is False
        assert not tectonic.binary_path().exists()
        assert "archive rejected" in str(tectonic.provision_state()["error"])
        assert not (data_root.parent / "escape").exists()

    def test_an_archive_without_the_binary_is_refused(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow_download(monkeypatch)
        body = _tar_gz({"README.md": b"not a compiler"})
        _pin(monkeypatch, _asset_for(body, name="t.tar.gz"))
        monkeypatch.setattr(tectonic, "binary_name", lambda: "tectonic")
        with _serve(body):
            assert tectonic.provision_sync(attempts=1) is False
        assert not tectonic.binary_path().exists()

    def test_an_implausibly_small_binary_is_refused(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stub named ``tectonic`` must not be installed as a compiler."""
        _allow_download(monkeypatch)
        body = _tar_gz({"tectonic": b"#!/bin/sh\nexit 0\n"})
        _pin(monkeypatch, _asset_for(body, name="t.tar.gz"))
        monkeypatch.setattr(tectonic, "binary_name", lambda: "tectonic")
        with _serve(body):
            assert tectonic.provision_sync(attempts=1) is False
        assert not tectonic.binary_path().exists()

    def test_a_zip_asset_is_installed_too(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow_download(monkeypatch)
        body = _zip_of({"tectonic.exe": _payload()})
        _pin(monkeypatch, _asset_for(body, name="t.zip"))
        monkeypatch.setattr(tectonic, "binary_name", lambda: "tectonic.exe")
        with _serve(body):
            assert tectonic.provision_sync(attempts=1) is True
        assert tectonic.binary_path().name == "tectonic.exe"
        assert tectonic.binary_path().is_file()

    def test_an_unsupported_platform_degrades_with_an_actionable_message(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow_download(monkeypatch)
        monkeypatch.setattr(tectonic, "current_asset", lambda: None)
        assert tectonic.provision_sync(attempts=1) is False
        error = str(tectonic.provision_state()["error"])
        assert "TeX Live" in error, "must still point at the manual route"
        assert not tectonic.binary_path().exists()

    def test_a_failed_download_leaves_nothing_behind(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A partial install that later 'looks valid' is the failure mode to avoid."""
        _allow_download(monkeypatch)
        _pin(monkeypatch, _asset_for(b"x", name="t.tar.gz"))
        opener = mock.MagicMock()
        opener.open.side_effect = tectonic.urllib.error.URLError("connection reset")
        with mock.patch.object(tectonic.urllib.request, "build_opener", return_value=opener):
            assert tectonic.provision_sync(attempts=1) is False
        assert not tectonic.binary_path().exists()
        # No staging or work directory survives.
        vendor = tectonic.vendor_dir()
        leftovers = list(vendor.glob("*")) if vendor.is_dir() else []
        assert leftovers == [], f"partial install left {leftovers}"
        assert "download failed" in str(tectonic.provision_state()["error"])

    def test_is_idempotent_when_already_installed(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow_download(monkeypatch)
        target = tectonic.binary_path()
        target.parent.mkdir(parents=True)
        target.write_bytes(_payload())
        target.chmod(0o755)
        opener = mock.MagicMock()
        with mock.patch.object(
            tectonic.urllib.request, "build_opener", return_value=opener
        ) as build:
            assert tectonic.provision_sync(attempts=1) is True
        assert build.call_count == 0, "must not re-download an installed compiler"

    def test_the_skip_env_refuses_before_any_socket(self, data_root: Path) -> None:
        """The escape hatch a test run relies on: no network, ever."""
        opener = mock.MagicMock()
        with mock.patch.object(
            tectonic.urllib.request, "build_opener", return_value=opener
        ) as build:
            assert tectonic.provision_sync(attempts=1) is False
        assert build.call_count == 0
        assert tectonic.SKIP_DOWNLOAD_ENV in str(tectonic.provision_state()["error"])

    def test_retries_a_transient_failure(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow_download(monkeypatch)
        monkeypatch.setattr(tectonic.time, "sleep", lambda _s: None)
        body = _tar_gz({"tectonic": _payload()})
        _pin(monkeypatch, _asset_for(body, name="t.tar.gz"))
        monkeypatch.setattr(tectonic, "binary_name", lambda: "tectonic")
        opener = mock.MagicMock()
        opener.open.side_effect = [
            tectonic.urllib.error.URLError("flaky"),
            _FakeResponse(body),
        ]
        with mock.patch.object(tectonic.urllib.request, "build_opener", return_value=opener):
            assert tectonic.provision_sync(attempts=2) is True
        assert tectonic.binary_installed() is True
        assert tectonic.provision_state()["attempt"] == 2

    def test_reports_download_progress(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow_download(monkeypatch)
        body = _tar_gz({"tectonic": _payload()})
        _pin(monkeypatch, _asset_for(body, name="t.tar.gz"))
        monkeypatch.setattr(tectonic, "binary_name", lambda: "tectonic")
        with _serve(body):
            assert tectonic.provision_sync(attempts=1) is True
        state = tectonic.provision_state()
        assert int(state["bytes_downloaded"]) == len(body)  # type: ignore[arg-type]


class TestBackgroundProvisioning:
    def test_runs_on_a_daemon_thread_and_resets_the_compiler_cache(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful install MUST drop the cached negative answer, or the very
        next compile still reports "no compiler"."""
        _allow_download(monkeypatch)
        body = _tar_gz({"tectonic": _payload()})
        _pin(monkeypatch, _asset_for(body, name="t.tar.gz"))
        monkeypatch.setattr(tectonic, "binary_name", lambda: "tectonic")
        # Prime the cache with the "nothing installed" answer.
        with mock.patch.object(latex.shutil, "which", return_value=None), mock.patch(
            "glob.glob", return_value=[]
        ):
            assert latex.find_compiler_sync() is None
        assert latex._compiler_cache == ""
        with _serve(body):
            assert tectonic.provision_in_background() is True
            thread = tectonic._job_thread
            assert thread is not None
            assert thread.daemon is True, "a 22MB transfer must not pin interpreter exit"
            thread.join(timeout=30)
        assert tectonic.binary_installed() is True
        assert latex._compiler_cache is None, "the stale negative answer was not dropped"

    def test_a_second_start_while_running_is_refused(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow_download(monkeypatch)
        release = tectonic.threading.Event()
        _pin(monkeypatch, _asset_for(b"x", name="t.tar.gz"))

        def _blocking_provision(_root: Path | None = None, attempts: int = 1) -> bool:
            release.wait(timeout=10)
            return False

        with mock.patch.object(tectonic, "provision_sync", _blocking_provision):
            assert tectonic.provision_in_background() is True
            assert tectonic.provision_in_background() is False, "one job at a time"
            release.set()
            thread = tectonic._job_thread
            assert thread is not None
            thread.join(timeout=10)

    def test_a_crashing_job_reports_instead_of_vanishing(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow_download(monkeypatch)
        with mock.patch.object(
            tectonic, "provision_sync", side_effect=RuntimeError("boom")
        ):
            assert tectonic.provision_in_background() is True
            thread = tectonic._job_thread
            assert thread is not None
            thread.join(timeout=10)
        state = tectonic.provision_state()
        assert state["state"] == tectonic.STATE_ERROR
        assert "boom" in str(state["error"])


@pytest.mark.asyncio
class TestEventLoopDiscipline:
    async def test_no_download_runs_on_the_event_loop(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``provision_in_background`` must hand the transfer to another thread.

        Asserted by recording the thread the blocking work runs on and proving it
        is NOT the loop's thread — the ``no-blocking-call-on-event-loop`` rule.
        """
        _allow_download(monkeypatch)
        loop_thread = tectonic.threading.get_ident()
        seen: list[int] = []

        def _record(_root: Path | None = None, attempts: int = 1) -> bool:
            seen.append(tectonic.threading.get_ident())
            return False

        with mock.patch.object(tectonic, "provision_sync", _record):
            assert tectonic.provision_in_background() is True
            thread = tectonic._job_thread
            assert thread is not None
            await asyncio.to_thread(thread.join, 10)
        assert seen and seen[0] != loop_thread

    async def test_managed_status_is_serializable_from_the_loop(
        self, data_root: Path
    ) -> None:
        status = await asyncio.to_thread(tectonic.managed_status)
        assert set(status) == {"supported", "installed", "release", "version", "job"}
        assert status["release"] == tectonic.TECTONIC_RELEASE_TAG


# ── resolution order ────────────────────────────────────────────────────────


class TestResolutionOrder:
    def test_a_users_own_pdflatex_still_wins(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The managed install must NEVER displace a real TeX distribution."""
        target = tectonic.binary_path()
        target.parent.mkdir(parents=True)
        target.write_bytes(_payload())
        target.chmod(0o755)
        assert tectonic.binary_installed() is True
        with mock.patch.object(latex.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"), \
                mock.patch.object(latex.os.path, "isfile", return_value=True), \
                mock.patch.object(latex.os, "access", return_value=True):
            assert latex.find_compiler_sync() == "/usr/bin/pdflatex"

    def test_a_userspace_texlive_install_still_wins(
        self, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        found = "/home/u/texlive/2026/bin/x86_64-linux/pdflatex"
        target = tectonic.binary_path()
        target.parent.mkdir(parents=True)
        target.write_bytes(_payload())
        target.chmod(0o755)
        with mock.patch.object(latex.shutil, "which", return_value=None), \
                mock.patch("glob.glob", side_effect=lambda p: [found] if "texlive" in p else []), \
                mock.patch.object(latex.os.path, "isfile", return_value=True), \
                mock.patch.object(latex.os, "access", return_value=True):
            assert latex.find_compiler_sync() == found

    def test_the_managed_install_is_used_when_nothing_else_exists(
        self, data_root: Path
    ) -> None:
        """The whole point: a stock machine compiles after one provision."""
        target = tectonic.binary_path()
        target.parent.mkdir(parents=True)
        target.write_bytes(_payload())
        target.chmod(0o755)
        with mock.patch.object(latex.shutil, "which", return_value=None), mock.patch(
            "glob.glob", return_value=[]
        ):
            assert latex.find_compiler_sync() == str(target)

    def test_still_reports_none_when_nothing_is_installed_anywhere(
        self, data_root: Path
    ) -> None:
        with mock.patch.object(latex.shutil, "which", return_value=None), mock.patch(
            "glob.glob", return_value=[]
        ):
            assert latex.find_compiler_sync() is None

    def test_the_managed_binary_is_driven_as_tectonic(self, data_root: Path) -> None:
        """``_compiler_argv`` keys off the basename, so the managed path must land in
        the tectonic branch — and must never be handed a shell-escape flag."""
        argv = latex._compiler_argv(
            str(tectonic.binary_path()), Path("/p/main.tex"), Path("/p")
        )
        assert "--keep-logs" in argv
        assert not any("shell-escape" in a for a in argv)

    def test_the_no_compiler_message_offers_the_managed_install(self) -> None:
        assert "Tectonic" in latex.NO_COMPILER_LOG
        # The manual route stays, for a host with no pinned build.
        assert "TeX Live" in latex.NO_COMPILER_LOG
