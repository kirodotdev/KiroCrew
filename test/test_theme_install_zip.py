"""Installing a theme from an uploaded ``.zip`` (``POST /api/themes/install-zip``).

``theme_zip.extract_theme_zip`` turns the archive into a directory, and the
handler hands that directory to the same install pipeline a local folder uses.
These tests cover what only an archive can express (traversal names, symlink
entries, size inflation, macOS metadata, a wrapping folder) and the HTTP surface
(size cap, empty body, governance, a pack landing under ``themes/``).

Every test points ``KIROCREW_HOME`` at ``tmp_path``. The HTTP tests run a real
aiohttp app on an OS-assigned loopback port.
"""

from __future__ import annotations

import io
import json
import os
import stat
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.platform.governance_profiles as gov_mod
from kiro_crew.dashboard import theme_zip
from kiro_crew.dashboard.handlers import themes as th

_VARS = {
    "dark": {"--bg": "#000000", "--text": "#ffffff", "--accent": "#3366ff"},
    "light": {"--bg": "#ffffff", "--text": "#000000", "--accent": "#0033cc"},
}
_MANIFEST = {"slug": "zipped", "name": "Zipped", "emoji": "🎃", "level": 0, "formatVersion": 1}


def _zip(entries: dict[str, bytes | str], *, symlinks: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = zipfile.ZipInfo(name)
            # On Windows the constructor rewrites a backslash to ``/``; store the
            # raw name so an uploaded archive's bytes are the same on every host.
            info.filename = name
            if name in symlinks:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            else:
                info.external_attr = (stat.S_IFREG | 0o644) << 16
            zf.writestr(info, data)
    return buf.getvalue()


def _one_member_zip(
    method: int, *, name: str = "theme.json", payload: bytes = b"a" * 4096
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload, compress_type=method)
    return buf.getvalue()


def _corrupt_member_zip(method: int) -> bytes:
    """A zip whose one *method* member's stream is garbage past its first bytes."""
    data = bytearray(_one_member_zip(method))
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
        info = zf.getinfo("theme.json")
    assert info.compress_type == method
    start = info.header_offset + 30 + len(info.filename) + len(info.extra)
    data[start + 4 : start + info.compress_size] = b"\x55" * (info.compress_size - 4)
    return bytes(data)


def _corrupt_bzip2_zip() -> bytes:
    return _corrupt_member_zip(zipfile.ZIP_BZIP2)


def _under_reported_lzma_zip() -> bytes:
    """~2.5 KB of LZMA expanding to 16 MiB, its headers claiming 10 bytes."""
    data = bytearray(_one_member_zip(zipfile.ZIP_LZMA, payload=b"\x00" * (16 * 1024 * 1024)))
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
        info = zf.getinfo("theme.json")
    small = (10).to_bytes(4, "little")
    cdir = data.rfind(b"PK\x01\x02")
    data[info.header_offset + 22 : info.header_offset + 26] = small
    data[cdir + 24 : cdir + 28] = small
    return bytes(data)


def _truncated_member_zip() -> bytes:
    """A zip whose stored member declares more bytes than the archive holds.

    Python 3.12+ refuses the read as an overlapping entry (``BadZipFile``);
    older versions run out of data mid-member (``EOFError``).
    """
    data = bytearray(_one_member_zip(zipfile.ZIP_STORED, payload=b"x" * 50))
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
        info = zf.getinfo("theme.json")
    declared = (len(data) + 4096).to_bytes(4, "little")
    cdir = data.rfind(b"PK\x01\x02")
    for off in (info.header_offset + 18, info.header_offset + 22, cdir + 20, cdir + 24):
        data[off : off + 4] = declared
    return bytes(data)


_LONG_COMPONENT = "a" * 300


def _pack_entries(prefix: str = "") -> dict[str, bytes | str]:
    return {
        f"{prefix}theme.json": json.dumps(_MANIFEST),
        f"{prefix}variables.json": json.dumps(_VARS),
    }


@pytest.fixture
def themes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    d = home / "themes"
    d.mkdir()
    assert os.path.realpath(str(th._themes_dir())) == os.path.realpath(str(d))
    return d


@pytest.fixture
def allow_install(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Allowed:
        permitted = True
        rule = "capabilities.theme_install"
        layer = "standalone"
        reason = ""

    monkeypatch.setattr(gov_mod, "governance_permits", lambda *a, **k: _Allowed())


class TestExtractThemeZip:
    def test_pack_at_the_archive_root(self, tmp_path: Path) -> None:
        root = theme_zip.extract_theme_zip(_zip(_pack_entries()), tmp_path)
        assert root == tmp_path
        assert (root / "theme.json").is_file()

    def test_pack_wrapped_in_one_folder_returns_that_folder(self, tmp_path: Path) -> None:
        entries = _pack_entries("haunted/")
        entries["__MACOSX/haunted/._theme.json"] = b"\x00\x05\x16\x07"
        entries["haunted/.DS_Store"] = b"junk"
        root = theme_zip.extract_theme_zip(_zip(entries), tmp_path)
        assert root == tmp_path / "haunted"
        assert sorted(p.name for p in root.iterdir()) == ["theme.json", "variables.json"]
        assert not (tmp_path / "__MACOSX").exists()

    @pytest.mark.parametrize(
        "name",
        [
            "../escape.json",
            "pack/../../escape.json",
            "/etc/escape.json",
            "C:/escape.json",
            "pack/D:x.json",
            "pack/x.json:stream",
            "a\\b.json",
        ],
    )
    def test_entries_that_leave_the_directory_are_refused(self, tmp_path: Path, name: str) -> None:
        dest = tmp_path / "dest"
        dest.mkdir()
        entries = _pack_entries()
        entries[name] = "x"
        with pytest.raises(ValueError):
            theme_zip.extract_theme_zip(_zip(entries), dest)
        assert not (tmp_path / "escape.json").exists()
        assert list(dest.iterdir()) == []
        assert sorted(p.name for p in tmp_path.iterdir()) == ["dest"]

    @pytest.mark.parametrize("kind", ["file", "directory", "macos", "ds-store", "appledouble"])
    def test_over_deep_entries_are_refused_before_writing(self, tmp_path: Path, kind: str) -> None:
        parts = ["a"] * theme_zip._MAX_PATH_PARTS + ["x"]
        if kind == "macos":
            parts[0] = "__MACOSX"
        elif kind == "ds-store":
            parts[-1] = ".DS_Store"
        elif kind == "appledouble":
            parts[-1] = "._x"
        name = "/".join(parts) + ("/" if kind == "directory" else "")
        entries = _pack_entries("pack/")
        entries[name] = "x"
        with pytest.raises(ValueError, match="path is too deep"):
            theme_zip.extract_theme_zip(_zip(entries), tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_an_entry_at_the_depth_limit_extracts(self, tmp_path: Path) -> None:
        name = "/".join(["pack"] + ["a"] * (theme_zip._MAX_PATH_PARTS - 2) + ["x.ttf"])
        entries = _pack_entries("pack/")
        entries[name] = b"font"
        root = theme_zip.extract_theme_zip(_zip(entries), tmp_path)
        assert root == tmp_path / "pack"
        assert (tmp_path / name).read_bytes() == b"font"

    def test_wrapped_fonts_extract_at_the_minimum_valid_depth_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        name = "pack/styles/fonts/x.ttf"
        assert len(Path(name).parts) <= theme_zip._MAX_PATH_PARTS
        monkeypatch.setattr(theme_zip, "_MAX_PATH_PARTS", len(Path(name).parts))
        entries = _pack_entries("pack/")
        entries[name] = b"font"
        root = theme_zip.extract_theme_zip(_zip(entries), tmp_path)
        assert root == tmp_path / "pack"
        assert (root / "styles/fonts/x.ttf").read_bytes() == b"font"

    def test_an_entry_resolving_outside_the_directory_is_refused(self, tmp_path: Path) -> None:
        # The name is clean, but a path component under dest resolves elsewhere;
        # the write must be refused rather than followed.
        dest = tmp_path / "dest"
        dest.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        try:
            (dest / "pack").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported on this platform")
        with pytest.raises(ValueError, match="leaves the archive"):
            theme_zip.extract_theme_zip(_zip({"pack/theme.json": "{}"}), dest)
        assert list(outside.iterdir()) == []

    def test_a_backslash_in_the_raw_name_is_refused_when_zipfile_rewrote_it(self) -> None:
        # What zipfile hands back on Windows: ``filename`` already uses ``/``
        # and only ``orig_filename`` still carries the backslash.
        info = zipfile.ZipInfo("a/b.json")
        info.orig_filename = "a\\b.json"
        with pytest.raises(ValueError, match="unsupported name"):
            theme_zip._entry_parts(info)

    def test_a_folder_opened_for_writing_as_permission_error_is_a_collision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Windows refuses ``open(<a folder>, "xb")`` with PermissionError rather
        # than IsADirectoryError, so the collision must be recognised from it too.
        real_open = open

        def _windows_open(path: object, mode: str = "r", *a: object, **k: object) -> object:
            if "x" in mode and Path(str(path)).is_dir():
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, mode, *a, **k)  # type: ignore[call-overload]

        monkeypatch.setattr(theme_zip, "open", _windows_open, raising=False)
        data = _zip({"pack/a/x.json": "{}", "pack/a": "x"})
        with pytest.raises(ValueError, match="a file and a folder with the same name"):
            theme_zip.extract_theme_zip(data, tmp_path)

    @pytest.mark.parametrize(
        "names",
        [("pack/a.json", "pack/a.json/b.json"), ("pack/a/b.json", "pack/a")],
    )
    def test_a_file_and_a_folder_of_the_same_name_is_refused(
        self, tmp_path: Path, names: tuple[str, str]
    ) -> None:
        with pytest.raises(ValueError, match="a file and a folder with the same name"):
            theme_zip.extract_theme_zip(_zip({names[0]: "x", names[1]: "y"}), tmp_path)

    def test_a_corrupt_deflate_member_is_a_value_error(self, tmp_path: Path) -> None:
        # ``_zip`` stores entries uncompressed, so build a real deflate member:
        # garbage in its stream surfaces as ``zlib.error``, not ``BadZipFile``.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("theme.json", b"a" * 4096, compress_type=zipfile.ZIP_DEFLATED)
        data = bytearray(buf.getvalue())
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
            info = zf.getinfo("theme.json")
        assert info.compress_type == zipfile.ZIP_DEFLATED
        start = info.header_offset + 30 + len(info.filename) + len(info.extra)
        data[start : start + info.compress_size] = b"\xff" * info.compress_size
        with pytest.raises(ValueError, match="cannot read archive entry"):
            theme_zip.extract_theme_zip(bytes(data), tmp_path)

    @pytest.mark.parametrize(
        "make",
        [
            _under_reported_lzma_zip,
            lambda: _corrupt_member_zip(zipfile.ZIP_LZMA),
            _corrupt_bzip2_zip,
            lambda: _one_member_zip(zipfile.ZIP_BZIP2),
        ],
        ids=["lzma-under-reported", "lzma-corrupt", "bzip2-corrupt", "bzip2-valid"],
    )
    def test_bzip2_and_lzma_members_are_refused_before_decompression(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Callable[[], bytes]
    ) -> None:
        # zipfile cannot bound a BZIP2 or LZMA read: a few KB can expand to
        # many MiB in memory before the byte budget counts anything.
        data = make()

        def _must_not_open(*a: object, **k: object) -> None:
            raise AssertionError("an unsupported member must be refused before it is opened")

        monkeypatch.setattr(zipfile.ZipFile, "open", _must_not_open)
        with pytest.raises(ValueError, match="unsupported compression method"):
            theme_zip.extract_theme_zip(data, tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_a_truncated_member_is_a_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="cannot read archive entry"):
            theme_zip.extract_theme_zip(_truncated_member_zip(), tmp_path)

    @pytest.mark.parametrize(
        "error",
        [EOFError(), OSError("Invalid data stream")],
        ids=["eof", "os-error"],
    )
    def test_a_member_read_failure_is_a_value_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        # EOFError is what zipfile raises when the archive runs out mid-member;
        # Python 3.12+ catches a real truncation earlier, so raise it directly.
        def _fails(self: zipfile.ZipExtFile, n: int = -1) -> bytes:
            raise error

        monkeypatch.setattr(zipfile.ZipExtFile, "read", _fails)
        with pytest.raises(ValueError, match="cannot read archive entry"):
            theme_zip.extract_theme_zip(_zip(_pack_entries()), tmp_path)

    def test_an_unsupported_zip_version_is_a_value_error(self, tmp_path: Path) -> None:
        # ``zipfile`` raises NotImplementedError while reading the directory.
        data = bytearray(_zip(_pack_entries()))
        cdir = data.find(b"PK\x01\x02")
        data[cdir + 6] = 130  # "version needed to extract" 13.0
        with pytest.raises(ValueError, match="not a valid .zip"):
            theme_zip.extract_theme_zip(bytes(data), tmp_path)

    @pytest.mark.parametrize(
        "name",
        [f"pack/{_LONG_COMPONENT}.json", f"{_LONG_COMPONENT}/theme.json"],
        ids=["file", "folder"],
    )
    def test_an_over_long_name_component_is_a_value_error(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(ValueError, match="archive entry") as caught:
            theme_zip.extract_theme_zip(_zip({name: "{}"}), tmp_path)
        assert isinstance(caught.value.__cause__, OSError)
        assert str(tmp_path) not in str(caught.value)

    def test_a_symlink_entry_is_refused(self, tmp_path: Path) -> None:
        entries = _pack_entries()
        entries["link"] = "/etc/passwd"
        with pytest.raises(ValueError, match="not a regular file"):
            theme_zip.extract_theme_zip(_zip(entries, symlinks=("link",)), tmp_path)
        assert not (tmp_path / "link").exists()

    def test_declared_size_over_the_cap_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(theme_zip, "_MAX_UNPACKED_BYTES", 100)
        entries = _pack_entries()
        entries["big.txt"] = "a" * 200
        with pytest.raises(ValueError, match="more than a theme"):
            theme_zip.extract_theme_zip(_zip(entries), tmp_path)
        assert not (tmp_path / "big.txt").exists()

    def test_a_header_that_under_reports_its_size_cannot_inflate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The declared total passes; the bytes actually written are counted too.
        monkeypatch.setattr(theme_zip, "_MAX_UNPACKED_BYTES", 1000)
        data = _zip({"big.txt": "a" * 5000})
        real_infolist = zipfile.ZipFile.infolist

        def _lying_infolist(self: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
            infos = real_infolist(self)
            for info in infos:
                info.file_size = 10
            return infos

        monkeypatch.setattr(zipfile.ZipFile, "infolist", _lying_infolist)
        with pytest.raises(ValueError):
            theme_zip.extract_theme_zip(data, tmp_path)

    def test_too_many_entries_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(theme_zip, "_MAX_ENTRIES", 2)
        entries = _pack_entries()
        entries["extra.txt"] = "x"
        data = _zip(entries)

        def _must_not_parse(*a: object, **k: object) -> None:
            raise AssertionError("the inventory must be vetted before zipfile parses it")

        monkeypatch.setattr(theme_zip.zipfile, "ZipFile", _must_not_parse)
        with pytest.raises(ValueError, match="too many entries"):
            theme_zip.extract_theme_zip(data, tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_not_a_zip(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not a valid .zip"):
            theme_zip.extract_theme_zip(b"definitely not a zip", tmp_path)


async def _post(data: bytes) -> tuple[int, dict]:
    app = web.Application()
    app.router.add_post("/api/themes/install-zip", th.api_themes_install_zip)
    async with TestClient(TestServer(app, host="127.0.0.1", port=0)) as client:
        resp = await client.post(
            "/api/themes/install-zip",
            data=data,
            headers={"Content-Type": "application/zip"},
        )
        return resp.status, await resp.json()


class TestInstallZipRoute:
    @pytest.mark.asyncio
    async def test_installs_a_wrapped_pack(self, themes_dir: Path, allow_install: None) -> None:
        status, body = await _post(_zip(_pack_entries("Zipped Theme/")))
        assert status == 200, body
        assert body["slug"] == "zipped"
        assert (themes_dir / "zipped" / "theme.json").is_file()

    @pytest.mark.asyncio
    async def test_a_refused_archive_installs_nothing(
        self, themes_dir: Path, allow_install: None
    ) -> None:
        entries = _pack_entries()
        entries["../escape.json"] = "x"
        status, body = await _post(_zip(entries))
        assert status == 400
        assert "leaves the archive" in body["error"]
        assert body["code"] == "theme_zip_invalid"
        assert list(themes_dir.iterdir()) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make",
        [
            _corrupt_bzip2_zip,
            _under_reported_lzma_zip,
            _truncated_member_zip,
            lambda: _zip({f"pack/{_LONG_COMPONENT}.json": "{}"}),
            lambda: _zip({f"{_LONG_COMPONENT}/theme.json": "{}"}),
        ],
        ids=["bzip2", "lzma", "truncated", "long-file-name", "long-folder-name"],
    )
    async def test_an_unreadable_member_is_400(
        self, themes_dir: Path, allow_install: None, make: Callable[[], bytes]
    ) -> None:
        status, body = await _post(make())
        assert status == 400, body
        assert body["code"] == "theme_zip_invalid"
        assert "archive entry" in body["error"]
        assert list(themes_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_deep_archive_is_400_and_leaves_no_temporary_tree(
        self, themes_dir: Path, allow_install: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        made: list[Path] = []
        real_mkdtemp = th.tempfile.mkdtemp

        def _recording_mkdtemp(*, prefix: str) -> str:
            path = real_mkdtemp(prefix=prefix)
            made.append(Path(path))
            return path

        monkeypatch.setattr(th.tempfile, "mkdtemp", _recording_mkdtemp)
        entries = _pack_entries()
        entries["a/" * 800 + "x"] = "x"
        entries["a/" * 1600 + "y"] = "y"
        status, body = await _post(_zip(entries))
        assert status == 400, body
        assert body["code"] == "theme_zip_invalid"
        assert "path is too deep" in body["error"]
        assert made and all(not p.exists() for p in made)
        assert list(themes_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_an_invalid_pack_reports_the_validator_error(
        self, themes_dir: Path, allow_install: None
    ) -> None:
        status, body = await _post(_zip({"readme.md": "no manifest here"}))
        assert status == 400
        assert body["error"]
        assert list(themes_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_empty_body_is_400(self, themes_dir: Path, allow_install: None) -> None:
        status, body = await _post(b"")
        assert status == 400
        assert body["error"] == "no theme archive was uploaded"
        assert body["code"] == "theme_zip_empty"

    @pytest.mark.asyncio
    async def test_oversized_upload_is_413(
        self, themes_dir: Path, allow_install: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(th, "THEME_ZIP_MAX_UPLOAD_BYTES", 64)
        status, body = await _post(_zip(_pack_entries()))
        assert status == 413
        assert body["code"] == "theme_zip_too_large"
        assert list(themes_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_governance_denial_is_403(
        self, themes_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Denied:
            permitted = False
            rule = "capabilities.theme_install"
            layer = "policy"
            reason = "theme installs are disabled here"

        monkeypatch.setattr(gov_mod, "governance_permits", lambda *a, **k: _Denied())
        status, body = await _post(_zip(_pack_entries()))
        assert status == 403
        assert body["error"] == "theme installs are disabled here"
        assert list(themes_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_the_temporary_unpack_is_removed(
        self, themes_dir: Path, allow_install: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        made: list[Path] = []
        real_mkdtemp = th.tempfile.mkdtemp

        def _recording_mkdtemp(*, prefix: str) -> str:
            path = real_mkdtemp(prefix=prefix)
            made.append(Path(path))
            return path

        monkeypatch.setattr(th.tempfile, "mkdtemp", _recording_mkdtemp)
        status, _ = await _post(_zip(_pack_entries()))
        assert status == 200
        assert made and all(not p.exists() for p in made)
