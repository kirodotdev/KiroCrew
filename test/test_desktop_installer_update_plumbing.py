"""The headless Windows desktop update's plumbing, exercised on any OS.

The Windows-only edges (the registry, PowerShell, the detached spawn, the
download) are replaced by fakes, so the refusal paths and the end-to-end
``prepare`` / ``hand_off`` sequence run on the Linux CI runners too.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
import types
from pathlib import Path

import pytest

from kiro_crew.platform import desktop_installer_update as desktop

ART = "https://download.crew.kiro.dev"
DATA = b"MZinstaller"
PUBLISHERS = ("Example Publisher",)
SIGNATURE = {"status": "Valid", "signer": PUBLISHERS[0], "timestamped": True}
APP_ID = "67c9cbdc-0708-5589-adc4-9c43c4e62ddb"


def _sha512(data: bytes) -> str:
    return base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def _feed(version: str = "0.13.1", data: bytes = DATA) -> bytes:
    url = f"{ART}/desktop/stable/{version}/KiroCrew-Setup.exe"
    sha = _sha512(data)
    return (
        f"version: {version}\nfiles:\n  - url: {url}\n    sha512: '{sha}'\n"
        f"    size: {len(data)}\npath: {url}\nsha512: '{sha}'\n"
    ).encode()


@pytest.fixture
def headless(monkeypatch, tmp_path):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KIROCREW_CDN_BASE", raising=False)
    monkeypatch.setattr(desktop, "_on_windows", lambda: True)
    return tmp_path


def _asar(*names: str) -> bytes:
    """An ``app.asar`` header listing *names* at its root, in Chromium pickle framing."""
    tree = json.dumps({"files": {n: {"size": 0, "offset": "0"} for n in names}}).encode()
    padded = tree + b"\0" * (-len(tree) % 4)
    header = struct.pack("<II", 4 + len(padded), len(tree)) + padded
    return struct.pack("<II", 4, len(header)) + header


def _install(root: Path, publisher: str = PUBLISHERS[0]) -> Path:
    res = root / "resources"
    (res / "backend-dist").mkdir(parents=True)
    (res / "app-update.yml").write_text(f"publisherName: {publisher}\n", encoding="utf-8")
    (res / "app.asar").write_bytes(_asar("main.js", "auto-update.js"))
    (root / "KiroCrew.exe").write_bytes(b"MZ")
    return root


# --- a registry stand-in -----------------------------------------------------------


class _Key:
    def __init__(self, path: tuple[str, ...]) -> None:
        self.path = path

    def __enter__(self) -> _Key:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _fake_winreg(tree: dict[tuple[str, ...], dict[str, str]]) -> types.ModuleType:
    """A ``winreg`` whose keys are the tuples of *tree* (hive first)."""
    mod = types.ModuleType("winreg")
    mod.HKEY_LOCAL_MACHINE = "HKLM"  # type: ignore[attr-defined]
    mod.HKEY_CURRENT_USER = "HKCU"  # type: ignore[attr-defined]

    def open_key(parent, sub):
        base = parent.path if isinstance(parent, _Key) else (parent,)
        path = (*base, *sub.split("\\"))
        if not any(k[: len(path)] == path for k in tree):
            raise OSError("no such key")
        return _Key(path)

    def enum_key(key, index):
        children = sorted(
            {
                k[len(key.path)]
                for k in tree
                if k[: len(key.path)] == key.path and len(k) > len(key.path)
            }
        )
        if index >= len(children):
            raise OSError("no more")
        return children[index]

    def query(key, name):
        values = tree.get(key.path, {})
        if name not in values:
            raise OSError("no such value")
        return values[name], 1

    mod.OpenKey = open_key  # type: ignore[attr-defined]
    mod.EnumKey = enum_key  # type: ignore[attr-defined]
    mod.QueryValueEx = query  # type: ignore[attr-defined]
    return mod


UNINSTALL = tuple(desktop._UNINSTALL_KEY.split("\\"))


def _registry(root: Path, hive: str = "HKLM") -> dict[tuple[str, ...], dict[str, str]]:
    return {
        (hive, *UNINSTALL, APP_ID): {
            "DisplayName": "KiroCrew 0.13.0",  # brand-ok
            "DisplayVersion": "0.13.0",
            "UninstallString": f'"{root / "Uninstall KiroCrew.exe"}" /allusers',  # brand-ok
        },
        (hive, *UNINSTALL, "OtherApp"): {
            "DisplayName": "Other",
            "DisplayVersion": "1.0",
            "UninstallString": "C:\\Other\\u.exe",
        },
        (hive, *UNINSTALL, "Broken"): {"DisplayName": "KiroCrew broken"},  # brand-ok
    }


class TestRegistry:
    def test_the_uninstall_entry_is_read(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(_registry(tmp_path)))
        entries = desktop._uninstall_entries()
        assert [(h, v) for h, v, _ in entries] == [("HKLM", "0.13.0")]
        assert desktop.install_scope(tmp_path) == desktop.SCOPE_ALL_USERS
        assert desktop.installed_version(tmp_path) == "0.13.0"

    def test_a_per_user_entry_is_the_current_user_scope(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(_registry(tmp_path, "HKCU")))
        assert desktop.install_scope(tmp_path) == desktop.SCOPE_CURRENT_USER

    def test_no_uninstall_key_reads_as_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({}))
        assert desktop._uninstall_entries() == []
        assert desktop.installed_version(tmp_path) is None

    def test_an_unreadable_registry_is_no_version(self, tmp_path, monkeypatch):
        monkeypatch.setattr(desktop, "_uninstall_entries", lambda: 1 / 0)
        assert desktop.installed_version(tmp_path) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="ctypes.windll exists on Windows")
    def test_off_windows_nobody_is_an_administrator(self):
        assert desktop._is_admin() is False


# --- settings, channel and source --------------------------------------------------


class TestSettings:
    def test_the_install_directory_is_found_from_the_package_or_not_at_all(self, tmp_path):
        assert desktop.install_dir(tmp_path) is None
        assert desktop.install_dir() is None or desktop.install_dir().is_dir()

    def test_a_single_publisher_name_is_accepted(self, tmp_path):
        _install(tmp_path)
        assert desktop.pinned_publishers(tmp_path) == PUBLISHERS

    def test_unreadable_updater_settings_are_a_signer_refusal(self, tmp_path):
        with pytest.raises(desktop.DesktopUpdateError) as err:
            desktop.pinned_publishers(tmp_path)
        assert err.value.code == desktop.ERR_SIGNER

    @staticmethod
    def _app_settings(monkeypatch, tmp_path, body: bytes | None) -> Path:
        """The desktop app's electron-store file under a fake ``%APPDATA%``."""
        from kiro_crew.platform import update_layout

        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        monkeypatch.setattr(update_layout, "policy_release_channel", lambda: "")
        path = tmp_path / "Roaming" / "kirocrew-desktop" / "config.json"
        if body is not None:
            path.parent.mkdir(parents=True)
            path.write_bytes(body)
        return path

    def test_a_release_build_follows_the_channel_chosen_in_the_app(self, monkeypatch, tmp_path):
        self._app_settings(monkeypatch, tmp_path, b'{"updateChannel": "insider", "x": 1}')
        assert desktop.desktop_channel("0.13.0") == "insider"

    @pytest.mark.parametrize(
        "body",
        [
            None,
            b"",
            b"{not json",
            b"\xff\xfe",
            b'["insider"]',
            b"{}",
            b'{"updateChannel": ""}',
            b'{"updateChannel": "beta"}',
            b'{"updateChannel": "lts"}',
            b'{"updateChannel": "nightly"}',
            b'{"updateChannel": "Insider"}',
            b'{"updateChannel": 5}',
            b'{"updateChannel": null}',
        ],
    )
    def test_a_missing_or_unusable_app_choice_is_stable(self, monkeypatch, tmp_path, body):
        self._app_settings(monkeypatch, tmp_path, body)
        assert desktop.app_channel_preference() == "stable"
        assert desktop.desktop_channel("0.13.0") == "stable"

    def test_an_oversized_settings_file_is_stable(self, monkeypatch, tmp_path):
        pad = b"x" * desktop._APP_SETTINGS_MAX_BYTES
        self._app_settings(
            monkeypatch, tmp_path, b'{"updateChannel": "insider", "p": "' + pad + b'"}'
        )
        assert desktop.desktop_channel("0.13.0") == "stable"

    def test_an_unreadable_settings_path_is_stable(self, monkeypatch, tmp_path):
        path = self._app_settings(monkeypatch, tmp_path, None)
        path.mkdir(parents=True)
        assert desktop.desktop_channel("0.13.0") == "stable"

    def test_the_data_home_channel_file_is_not_a_second_source(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        (tmp_path / "home" / "channel").write_text("insider\n", encoding="utf-8")
        self._app_settings(monkeypatch, tmp_path, b'{"updateChannel": "stable"}')
        assert desktop.desktop_channel("0.13.0") == "stable"

    @pytest.mark.parametrize(
        ("required", "expected"), [("stable", "stable"), ("lts", "lts"), ("nightly", None)]
    )
    def test_a_required_channel_wins_over_the_app_choice(
        self, monkeypatch, tmp_path, required, expected
    ):
        from kiro_crew.platform import update_layout

        self._app_settings(monkeypatch, tmp_path, b'{"updateChannel": "insider"}')
        monkeypatch.setattr(update_layout, "policy_release_channel", lambda: required)
        assert desktop.desktop_channel("0.13.0") == expected

    def test_the_settings_folder_is_the_release_apps_package_name(self):
        package = Path(__file__).resolve().parents[1] / "website" / "electron" / "package.json"
        name = json.loads(package.read_text(encoding="utf-8"))["name"]
        assert desktop.APP_SETTINGS_DIR == name

    def test_an_unsafe_or_blocked_source_is_refused(self, monkeypatch):
        from kiro_crew.platform import update_governance, update_layout

        monkeypatch.setattr(update_layout, "cdn_bases_are_safe", lambda: False)
        assert "safe HTTPS" in (desktop.source_refusal() or "")
        monkeypatch.setattr(update_layout, "cdn_bases_are_safe", lambda: True)
        monkeypatch.setattr(update_governance, "update_blocked_reason", lambda base: "pinned")
        assert desktop.source_refusal() == "pinned"
        monkeypatch.setattr(update_governance, "update_blocked_reason", lambda base: None)
        assert desktop.source_refusal() is None

    def test_the_feed_url_is_the_channels_latest(self, monkeypatch):
        from kiro_crew.platform import update_layout

        monkeypatch.setattr(update_layout, "cdn_bases", lambda: ("https://feed.test", ART))
        assert desktop.feed_url("stable") == "https://feed.test/feed/stable/latest.yml"

    def test_an_overridden_source_is_noticed(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_CDN_BASE", "https://mirror.test")
        assert desktop.source_overridden() is True


class TestFetchFeed:
    def _fetch(self, monkeypatch, result):
        from kiro_crew.dashboard.handlers import _update_feed

        async def fetch(url, *, timeout_secs, max_bytes):
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(_update_feed, "fetch_feed_bytes", fetch)
        return asyncio.run(desktop.fetch_feed("stable"))

    def test_a_good_feed_is_returned(self, headless, monkeypatch):
        assert self._fetch(monkeypatch, (200, _feed())).version == "0.13.1"

    @pytest.mark.parametrize("result", [(503, b""), OSError("down")])
    def test_an_unreachable_feed_is_refused(self, headless, monkeypatch, result):
        with pytest.raises(desktop.DesktopUpdateError) as err:
            self._fetch(monkeypatch, result)
        assert err.value.code == desktop.ERR_FEED_UNREACHABLE

    def test_an_oversized_feed_is_refused(self, headless):
        with pytest.raises(desktop.DesktopUpdateError):
            desktop.parse_feed(b"x" * (desktop.FEED_MAX_BYTES + 1), channel="stable")


# --- the signature probe -------------------------------------------------------------


class TestReadSignature:
    def _run(self, monkeypatch, *, stdout="", returncode=0, raises=None):
        seen = {}

        def run(argv, **kwargs):
            seen["argv"], seen["env"] = argv, kwargs["env"]
            if raises:
                raise raises
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

        monkeypatch.setattr(desktop, "_powershell", lambda: "powershell.exe")
        monkeypatch.setattr(desktop.subprocess, "run", run)
        return seen

    def test_the_probe_reads_the_path_from_the_environment(self, monkeypatch, tmp_path):
        seen = self._run(monkeypatch, stdout=json.dumps(SIGNATURE) + "\n")
        assert desktop.read_signature(tmp_path / "a b.exe") == SIGNATURE
        assert seen["env"]["KC_SIG_PATH"] == str(tmp_path / "a b.exe")
        assert str(tmp_path) not in " ".join(seen["argv"])

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"stdout": "not json"},
            {"stdout": json.dumps(SIGNATURE), "returncode": 1},
            {"raises": OSError("no powershell")},
            {"raises": subprocess.TimeoutExpired("ps", 1)},
        ],
    )
    def test_a_probe_that_fails_is_a_signature_refusal(self, monkeypatch, tmp_path, kwargs):
        self._run(monkeypatch, **kwargs)
        with pytest.raises(desktop.DesktopUpdateError) as err:
            desktop.read_signature(tmp_path / "x.exe")
        assert err.value.code == desktop.ERR_SIGNATURE

    def test_no_system_powershell_is_a_signature_refusal(self, monkeypatch):
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: None)
        with pytest.raises(desktop.DesktopUpdateError) as err:
            desktop._powershell()
        assert err.value.code == desktop.ERR_SIGNATURE

    @pytest.mark.parametrize(
        ("change", "code"),
        [({"status": "HashMismatch"}, "ERR_SIGNATURE"), ({"timestamped": False}, "ERR_SIGNATURE")],
    )
    def test_an_invalid_or_untimestamped_signature_is_refused(self, tmp_path, change, code):
        with pytest.raises(desktop.DesktopUpdateError) as err:
            desktop.verify_signature(
                tmp_path, PUBLISHERS, reader=lambda _p: {**SIGNATURE, **change}
            )
        assert err.value.code == getattr(desktop, code)


# --- the running-app probe -----------------------------------------------------------


class TestDesktopAppRunning:
    def _run(self, monkeypatch, *, stdout="", returncode=0, raises=None):
        seen = {}

        def run(argv, **kwargs):
            seen["argv"], seen["env"] = argv, kwargs["env"]
            if raises:
                raise raises
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

        monkeypatch.setattr(desktop, "_powershell", lambda: "powershell.exe")
        monkeypatch.setattr(desktop.subprocess, "run", run)
        return seen

    @pytest.mark.parametrize(("stdout", "running"), [("1\r\n", True), ("0\r\n", False)])
    def test_the_apps_executables_in_this_install_are_counted(
        self, headless, monkeypatch, stdout, running
    ):
        seen = self._run(monkeypatch, stdout=stdout)
        root = headless / "app dir"
        assert desktop.desktop_app_running(root) is running
        assert seen["env"]["KC_APP_PATHS"].split("\n") == [
            str(root / name) for name in desktop.APP_EXECUTABLES
        ]
        assert str(root) not in " ".join(seen["argv"])

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"stdout": "not a count"},
            {"stdout": "0", "returncode": 1},
            {"raises": OSError("no powershell")},
            {"raises": subprocess.TimeoutExpired("ps", 1)},
            {"raises": desktop.DesktopUpdateError(desktop.ERR_SIGNATURE, "no powershell")},
        ],
    )
    def test_a_probe_that_cannot_tell_counts_the_app_as_running(
        self, headless, monkeypatch, kwargs
    ):
        self._run(monkeypatch, **kwargs)
        assert desktop.desktop_app_running(headless) is True

    def test_off_windows_or_outside_an_install_nothing_is_probed(self, headless, monkeypatch):
        seen = self._run(monkeypatch, stdout="1")
        monkeypatch.setattr(desktop, "install_dir", lambda: None)
        assert desktop.desktop_app_running() is False
        monkeypatch.setattr(desktop, "_on_windows", lambda: False)
        assert desktop.desktop_app_running(headless) is False
        assert seen == {}


# --- the download ----------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, chunks: list[bytes]) -> None:
        self.status = status
        self.content = self
        self._chunks = chunks

    async def iter_chunked(self, _n):
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


def _fake_session(resp: _Resp | Exception):
    class Session:
        def __init__(self, **_kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def get(self, url, proxy=None):
            if isinstance(resp, Exception):
                raise resp
            return resp

    return Session


class TestDownload:
    def _download(self, monkeypatch, tmp_path, resp):
        import aiohttp

        monkeypatch.setattr(aiohttp, "ClientSession", _fake_session(resp))
        release = desktop.parse_feed(_feed(), channel="stable")
        dest = tmp_path / "out.part"
        asyncio.run(desktop._download(release, dest))
        return dest

    def test_the_installer_streams_to_the_destination(self, headless, monkeypatch):
        dest = self._download(monkeypatch, headless, _Resp(200, [DATA[:4], DATA[4:]]))
        assert dest.read_bytes() == DATA

    def test_an_error_status_is_a_download_refusal(self, headless, monkeypatch):
        with pytest.raises(desktop.DesktopUpdateError) as err:
            self._download(monkeypatch, headless, _Resp(404, []))
        assert err.value.code == desktop.ERR_DOWNLOAD

    def test_more_bytes_than_the_feed_says_is_a_digest_refusal(self, headless, monkeypatch):
        with pytest.raises(desktop.DesktopUpdateError) as err:
            self._download(monkeypatch, headless, _Resp(200, [DATA, b"extra"]))
        assert err.value.code == desktop.ERR_DIGEST

    def test_a_network_failure_is_a_download_refusal(self, headless, monkeypatch):
        with pytest.raises(desktop.DesktopUpdateError) as err:
            self._download(monkeypatch, headless, OSError("reset"))
        assert err.value.code == desktop.ERR_DOWNLOAD


# --- prepare and hand off, end to end --------------------------------------------------


@pytest.fixture
def host(headless, monkeypatch):
    """A Windows all-users install, its registry, and the stable feed offering 0.13.1."""
    root = _install(headless / "Program Files" / "KiroCrew")  # brand-ok
    monkeypatch.setattr(desktop, "install_dir", lambda: root)
    monkeypatch.setattr(desktop, "source_refusal", lambda: None)
    monkeypatch.setattr(desktop, "desktop_channel", lambda running: "stable")
    feed = {"raw": _feed()}

    async def fetch_feed(channel):
        return desktop.parse_feed(feed["raw"], channel=channel)

    monkeypatch.setattr(desktop, "fetch_feed", fetch_feed)
    registry = _registry(root)
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(registry))

    async def download(_release, dest: Path) -> None:
        dest.write_bytes(DATA)

    def prepare(running="0.13.0", **kw):
        return asyncio.run(
            desktop.prepare(
                running,
                download=download,
                signature_reader=lambda _p: dict(SIGNATURE),
                is_admin=lambda: True,
                **kw,
            )
        )

    return types.SimpleNamespace(root=root, feed=feed, registry=registry, prepare=prepare)


class TestExternallyManaged:
    """The two marker shapes the app's own updater stands down for, read the same way."""

    def test_an_install_with_no_marker_is_ours(self, tmp_path):
        assert desktop.externally_managed(_install(tmp_path)) is False

    def test_a_loose_marker_of_any_kind_is_managed(self, tmp_path):
        root = _install(tmp_path)
        (root / "resources" / desktop.EXTERNALLY_MANAGED_MARKER).write_bytes(b"")
        assert desktop.externally_managed(root) is True

    def test_a_loose_marker_directory_is_managed(self, tmp_path):
        root = _install(tmp_path)
        (root / "resources" / desktop.EXTERNALLY_MANAGED_MARKER).mkdir()
        assert desktop.externally_managed(root) is True

    def test_a_marker_baked_into_the_archive_is_managed(self, tmp_path):
        root = _install(tmp_path)
        archive = root / "resources" / "app.asar"
        archive.write_bytes(_asar("main.js", desktop.EXTERNALLY_MANAGED_MARKER))
        assert desktop.externally_managed(root) is True

    def test_a_nested_entry_of_that_name_is_not_the_marker(self, tmp_path):
        root = _install(tmp_path)
        tree = {"files": {"sub": {"files": {desktop.EXTERNALLY_MANAGED_MARKER: {"size": 0}}}}}
        raw = json.dumps(tree).encode()
        header = struct.pack("<II", 4 + len(raw) + (-len(raw) % 4), len(raw))
        header += raw + b"\0" * (-len(raw) % 4)
        (root / "resources" / "app.asar").write_bytes(struct.pack("<II", 4, len(header)) + header)
        assert desktop.externally_managed(root) is False

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"\x04\x00\x00\x00",
            struct.pack("<4I", 8, 16, 12, 8) + b"{}{}{}{}",
            struct.pack("<4I", 4, 12, 8, 64) + b"{}",
            struct.pack("<4I", 4, 16, 12, 6) + b"not js",
            struct.pack("<4I", 4, 16, 12, 2) + b"[]",
        ],
        ids=["empty", "short", "bad-pickle", "truncated", "not-json", "no-file-tree"],
    )
    def test_an_archive_that_cannot_be_read_counts_as_managed(self, tmp_path, data):
        root = _install(tmp_path)
        (root / "resources" / "app.asar").write_bytes(data)
        assert desktop.externally_managed(root) is True

    def test_no_archive_or_no_install_counts_as_managed(self, tmp_path, monkeypatch):
        root = _install(tmp_path)
        (root / "resources" / "app.asar").unlink()
        assert desktop.externally_managed(root) is True
        monkeypatch.setattr(desktop, "install_dir", lambda: None)
        assert desktop.externally_managed() is True

    def test_a_rewritten_archive_is_read_again(self, tmp_path):
        root = _install(tmp_path)
        archive = root / "resources" / "app.asar"
        assert desktop.externally_managed(root) is False
        archive.write_bytes(_asar("main.js", "x", desktop.EXTERNALLY_MANAGED_MARKER))
        assert desktop.externally_managed(root) is True


class TestPrepare:
    def test_an_externally_managed_install_downloads_nothing(self, host):
        (host.root / "resources" / desktop.EXTERNALLY_MANAGED_MARKER).write_text("{}")
        with pytest.raises(desktop.DesktopUpdateError) as err:
            host.prepare()
        assert err.value.code == desktop.ERR_EXTERNALLY_MANAGED

    def test_a_verified_installer_and_its_destination_are_prepared(self, host):
        prepared = host.prepare(expected_version="0.13.1")
        assert prepared.release.version == "0.13.1"
        assert prepared.installer.read_bytes() == DATA
        assert prepared.scope == desktop.SCOPE_ALL_USERS
        assert prepared.root == host.root
        assert prepared.app_exe == "KiroCrew.exe"

    def test_off_windows_nothing_is_prepared(self, host, monkeypatch):
        monkeypatch.setattr(desktop, "_on_windows", lambda: False)
        with pytest.raises(desktop.DesktopUpdateError) as err:
            host.prepare()
        assert err.value.code == desktop.ERR_NOT_WINDOWS

    def test_no_install_directory_is_a_refusal(self, host, monkeypatch):
        monkeypatch.setattr(desktop, "install_dir", lambda: None)
        with pytest.raises(desktop.DesktopUpdateError) as err:
            host.prepare()
        assert err.value.code == desktop.ERR_NO_INSTALL

    def test_a_refused_source_is_a_refusal(self, host, monkeypatch):
        monkeypatch.setattr(desktop, "source_refusal", lambda: "blocked by policy")
        with pytest.raises(desktop.DesktopUpdateError) as err:
            host.prepare()
        assert err.value.code == desktop.ERR_SOURCE

    def test_no_channel_is_a_refusal(self, host, monkeypatch):
        monkeypatch.setattr(desktop, "desktop_channel", lambda running: None)
        with pytest.raises(desktop.DesktopUpdateError) as err:
            host.prepare()
        assert err.value.code == desktop.ERR_FEED

    def test_a_feed_that_moved_since_the_arm_is_a_refusal(self, host):
        with pytest.raises(desktop.DesktopUpdateError) as err:
            host.prepare(expected_version="0.13.2")
        assert err.value.code == desktop.ERR_VERSION_CHANGED

    def test_a_directory_without_the_apps_executable_downloads_nothing(self, host):
        (host.root / "KiroCrew.exe").unlink()
        with pytest.raises(desktop.DesktopUpdateError) as err:
            host.prepare()
        assert err.value.code == desktop.ERR_INSTALL_DIR
        assert not desktop.staging_dir().exists() or not any(desktop.staging_dir().iterdir())


class TestHandOff:
    def test_the_helper_is_spawned_with_the_prepared_plan(self, host, monkeypatch):
        monkeypatch.setattr(desktop, "_powershell", lambda: "powershell.exe")
        monkeypatch.setattr(desktop, "restart_argv", lambda: ("python.exe", "-m", "kiro_crew"))
        prepared = host.prepare()
        spawned: list[list[str]] = []

        def spawn(argv, env=None):
            spawned.append(argv)
            return 4242

        assert desktop.hand_off(prepared, wait_pids=(11, 12), restart=True, spawn=spawn) == 4242
        script = base64.b64decode(spawned[0][-1]).decode("utf-16-le")
        assert "$Version = '0.13.1'" in script
        assert f"$InstallDir = '{host.root}'" in script
        assert "$AppExe = 'KiroCrew.exe'" in script

    @pytest.mark.parametrize("restart", [True, False])
    def test_only_a_restarting_helper_carries_what_this_gateways_successor_needs(
        self, host, monkeypatch, restart
    ):
        """A re-exec hands its successor the kept values (the .env ownership record).

        The helper starts this gateway's successor, so it must hand them on the
        same way; without them the successor would treat .env tokens it inherits
        as foreign and never drop one a person removes from .env.
        """
        from kiro_crew import platform_compat

        monkeypatch.setattr(desktop, "_powershell", lambda: "powershell.exe")
        monkeypatch.setattr(desktop, "restart_argv", lambda: ("python.exe", "-m", "kiro_crew"))
        monkeypatch.setattr(
            platform_compat, "kept_for_reexec", lambda: {"_KIROCREW_ENV_OWNED_KEYS": "SLACK_X"}
        )
        monkeypatch.delenv("_KIROCREW_ENV_OWNED_KEYS", raising=False)
        envs: list[object] = []

        def spawn(argv, env=None):
            envs.append(env)
            return 1

        desktop.hand_off(host.prepare(), wait_pids=(11,), restart=restart, spawn=spawn)
        if restart:
            assert isinstance(envs[0], dict)
            assert envs[0]["_KIROCREW_ENV_OWNED_KEYS"] == "SLACK_X"
            assert envs[0]["PATH"] == os.environ["PATH"]
        else:
            assert envs[0] is None
        assert "_KIROCREW_ENV_OWNED_KEYS" not in os.environ

    def test_the_restart_command_is_this_gateways_own(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["kirocrew", "start"])
        argv = desktop.restart_argv()
        assert argv[-4:] == ("-P", "-m", "kiro_crew", "start")

    def test_the_result_record_reads_back_and_clears(self, headless):
        assert desktop.read_result() is None
        desktop._result_path().parent.mkdir(parents=True, exist_ok=True)
        desktop._result_path().write_text("[]", encoding="utf-8")
        assert desktop.read_result() is None
        desktop._result_path().write_text('{"ok": true, "version": "0.13.1"}', encoding="utf-8")
        assert desktop.read_result() == {"ok": True, "version": "0.13.1"}
        assert desktop.result_error_for("0.13.1") is None
        desktop.clear_result()
        assert desktop.read_result() is None
        desktop.clear_result()

    def test_the_spawn_leaves_the_job_or_falls_back(self, monkeypatch):
        calls: list[int] = []

        class Proc:
            pid = 99

        def popen(argv, *, creationflags, **kwargs):
            calls.append(creationflags)
            if len(calls) == 1:
                raise OSError("breakaway refused")
            assert kwargs["stdin"] is subprocess.DEVNULL
            return Proc()

        monkeypatch.setattr(desktop.subprocess, "Popen", popen)
        assert desktop._spawn_detached(["x"]) == 99
        assert len(calls) == 2
