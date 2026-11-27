"""A headless Windows desktop install updates from its gateway.

Decision entry "a headless Windows desktop install updates from its gateway":
the Windows desktop bundle, run without the desktop app, reads the app's own
release feed, verifies the signed installer (feed sha512, Authenticode status,
timestamp and pinned signer, never a downgrade), and hands it to a detached
helper only at a start or stop a person caused. Every check fails closed.

Every Windows-only probe (the platform, the signature reader, the registry,
the helper spawn) is replaced by a recording stand-in, so the suite runs on any
host and nothing here starts a process or touches the network.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
import types
from pathlib import Path

import pytest

from kiro_crew import update_boot_apply, update_next_start
from kiro_crew.platform import desktop_installer_update as desktop
from kiro_crew.platform.update_capability import (
    MANAGED_BY_ELECTRON,
    auto_update_effect,
    derive_capability,
)

ART = "https://download.crew.kiro.dev"
GOOD_SIGNATURE = {
    "status": "Valid",
    "signer": "Example Publisher",
    "timestamped": True,
}
PUBLISHERS = ("Example Publisher",)


def _sha512(data: bytes) -> str:
    return base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def _feed(
    version: str = "0.13.1",
    *,
    channel: str = "stable",
    data: bytes = b"MZinstaller",
    url: str | None = None,
    sha: str | None = None,
    size: int | None = None,
) -> bytes:
    url = url or f"{ART}/desktop/{channel}/{version}/KiroCrew-Setup.exe"
    sha = sha or _sha512(data)
    size = len(data) if size is None else size
    return (
        f"version: {version}\nfiles:\n  - url: {url}\n    sha512: '{sha}'\n    size: {size}\n"
        f"path: {url}\nsha512: '{sha}'\nreleaseDate: '2026-11-22T02:51:32Z'\n"
    ).encode()


@pytest.fixture
def headless(monkeypatch, tmp_path):
    """The Windows desktop bundle with no desktop app hosting the gateway."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(update_boot_apply.DESKTOP_HOSTED_ENV, raising=False)
    monkeypatch.delenv("KIROCREW_CDN_BASE", raising=False)
    monkeypatch.setattr(desktop, "_on_windows", lambda: True)
    monkeypatch.setattr(desktop, "desktop_app_running", lambda root=None: False)
    monkeypatch.setattr(desktop, "externally_managed", lambda root=None: False)
    return tmp_path


#: The real shape check, for the tests whose fixture stands it in.
_HEADLESS_DESKTOP_INSTALL = desktop.headless_desktop_install


# --- capability derivation -----------------------------------------------------


class TestCapability:
    def test_a_headless_windows_desktop_install_offers_the_update(self, headless):
        cap = derive_capability(install_root="", dist="nsis")
        assert cap.managed_by == MANAGED_BY_ELECTRON
        assert cap.defers is False
        assert cap.can_apply is True
        assert cap.remediation == {
            "kind": "command",
            "message": "Update this install from a terminal on the host.",
            "command": "kirocrew update",
        }

    def test_a_desktop_hosted_gateway_still_defers_to_the_app(self, headless, monkeypatch):
        monkeypatch.setenv(update_boot_apply.DESKTOP_HOSTED_ENV, "1")
        cap = derive_capability(install_root="", dist="nsis")
        assert cap.unavailable_reason == "managed_by_app"
        assert cap.can_apply is False

    def test_the_same_bundle_off_windows_still_defers(self, headless, monkeypatch):
        monkeypatch.setattr(desktop, "_on_windows", lambda: False)
        assert derive_capability(install_root="", dist="nsis").defers is True

    @pytest.mark.parametrize("dist", ["dmg", "appimage", "deb", "rpm"])
    def test_other_desktop_stamps_keep_deferring(self, headless, dist):
        assert derive_capability(install_root="", dist=dist).defers is True

    def test_an_overridden_source_is_not_applied_from_the_dashboard(self, headless, monkeypatch):
        monkeypatch.setenv("KIROCREW_CDN_BASE", "https://mirror.example.test")
        cap = derive_capability(install_root="", dist="nsis")
        assert cap.defers is False
        assert cap.can_apply is False

    def test_the_unattended_loop_only_notifies(self, headless):
        effect = auto_update_effect(install_root="", dist="nsis", provider=None)
        assert effect is not None
        assert effect.effect == "notify"
        assert effect.route is None

    def test_an_externally_managed_install_keeps_deferring(self, headless, monkeypatch):
        """The operator's EXTERNALLY-MANAGED marker wins, as it does in the app's updater."""
        monkeypatch.setattr(desktop, "externally_managed", lambda root=None: True)
        assert desktop.headless_desktop_install("nsis") is False
        cap = derive_capability(install_root="", dist="nsis")
        assert cap.defers is True
        assert cap.can_apply is False and cap.remediation is None
        effect = auto_update_effect(install_root="", dist="nsis", provider=None)
        assert effect is not None and effect.reason == "its own updater owns this install"
        assert update_boot_apply._headless_desktop() is False


# --- the feed --------------------------------------------------------------------


class TestFeed:
    def test_a_valid_feed_parses(self, headless):
        release = desktop.parse_feed(_feed(), channel="stable")
        assert release.version == "0.13.1"
        assert release.url == f"{ART}/desktop/stable/0.13.1/KiroCrew-Setup.exe"
        assert release.size == len(b"MZinstaller")

    def test_the_insider_semver_parses(self, headless):
        assert desktop.parse_feed(_feed("0.14.0-rc.5", channel="insider"), channel="insider")

    def test_an_insider_feed_may_name_the_stable_installer_it_mirrors(self, headless):
        # The publish lane copies stable's feed to insider when stable advances it.
        release = desktop.parse_feed(_feed("0.13.1", channel="stable"), channel="insider")
        assert release.url == f"{ART}/desktop/stable/0.13.1/KiroCrew-Setup.exe"
        with pytest.raises(desktop.DesktopUpdateError):
            desktop.parse_feed(_feed("0.14.0-rc.5", channel="insider"), channel="stable")

    @pytest.mark.parametrize(
        ("required", "expected"),
        [("", "nightly"), ("nightly", "nightly"), ("stable", None), ("lts", None)],
    )
    def test_a_nightly_build_honours_a_required_channel(
        self, headless, monkeypatch, required, expected
    ):
        from kiro_crew.platform import update_layout

        monkeypatch.setattr(update_layout, "policy_release_channel", lambda: required)
        assert desktop.desktop_channel("0.15.0.dev20261126") == expected

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example.test/desktop/stable/0.13.1/KiroCrew-Setup.exe",
            f"{ART}/desktop/insider/0.13.1/KiroCrew-Setup.exe",
            f"{ART}/desktop/nightly/0.13.1/KiroCrew-Setup.exe",
            f"{ART}/desktop/stable/0.13.0/KiroCrew-Setup.exe",
            f"{ART}/desktop/stable/0.13.1/other.exe",
            "http://download.crew.kiro.dev/desktop/stable/0.13.1/KiroCrew-Setup.exe",
        ],
    )
    def test_an_installer_url_off_this_channel_is_refused(self, headless, url):
        with pytest.raises(desktop.DesktopUpdateError) as err:
            desktop.parse_feed(_feed(url=url), channel="stable")
        assert err.value.code == desktop.ERR_FEED

    @pytest.mark.parametrize(
        "raw",
        [
            b"[not, a, mapping]",
            b"version: 0.13.1\nfiles: []\n",
            b"version: '../x'\nfiles: []\n",
            b"\xff\xfe",
        ],
    )
    def test_a_malformed_feed_is_refused(self, headless, raw):
        with pytest.raises(desktop.DesktopUpdateError):
            desktop.parse_feed(raw, channel="stable")

    def test_a_trailing_newline_does_not_pass_the_version_or_digest_shape(self, headless):
        with pytest.raises(desktop.DesktopUpdateError):
            desktop.parse_feed(_feed(sha=_sha512(b"MZinstaller") + "\\n"), channel="stable")
        raw = _feed().replace(b"version: 0.13.1\n", b'version: "0.13.1\\n"\n', 1)
        with pytest.raises(desktop.DesktopUpdateError):
            desktop.parse_feed(raw, channel="stable")

    def test_a_bad_sha512_or_size_is_refused(self, headless):
        with pytest.raises(desktop.DesktopUpdateError):
            desktop.parse_feed(_feed(sha="not-a-digest"), channel="stable")
        with pytest.raises(desktop.DesktopUpdateError):
            desktop.parse_feed(_feed(size=0), channel="stable")

    def test_two_different_installers_are_refused(self, headless):
        raw = _feed().replace(
            f"path: {ART}/desktop/stable/0.13.1/KiroCrew-Setup.exe".encode(),
            f"path: {ART}/desktop/stable/0.13.1/KiroCrew-LTS-Setup.exe".encode(),
        )
        with pytest.raises(desktop.DesktopUpdateError):
            desktop.parse_feed(raw, channel="stable")

    @pytest.mark.parametrize("offered", ["0.13.0", "0.12.9", "0.13.0-rc.1"])
    def test_an_equal_or_older_release_is_a_downgrade_refusal(self, headless, offered):
        release = desktop.parse_feed(_feed(offered), channel="stable")
        with pytest.raises(desktop.DesktopUpdateError) as err:
            desktop.require_newer(release, "0.13.0")
        assert err.value.code == desktop.ERR_DOWNGRADE

    @pytest.mark.parametrize(
        ("candidate", "running", "newer"),
        [
            # Two nightlies of one day order by their time, in either spelling.
            ("0.15.0-nightly.20261127t180000", "0.15.0-nightly.20261127t090000", True),
            ("0.15.0-nightly.20261127t180000", "0.15.0.dev20261127180000", False),
            ("0.15.0-nightly.20261127t090000", "0.15.0-nightly.20261127t180000", False),
            # An edition's build of a release, from the shell or the backend.
            ("0.15.0-12", "0.15.0.11", True),
            ("0.15.0-11", "0.15.0.11", False),
            ("0.15.0", "0.15.0.11", False),
            # Channel order within a core: nightly < insider < release.
            ("0.14.0-rc.6", "0.14.0rc5", True),
            ("0.14.0", "0.14.0-rc.5", True),
            ("0.14.0-rc.5", "0.14.0", False),
            ("0.14.0-rc.1", "0.14.0-nightly.20261127t090000", True),
            ("0.13.1", "0.13.0", True),
        ],
    )
    def test_the_desktop_ordering(self, candidate, running, newer):
        assert desktop.is_newer(candidate, running) is newer

    def test_an_unordered_version_is_none(self):
        assert desktop.is_newer("latest", "0.13.0") is None
        assert desktop.is_newer("0.13.1", "0.13.0+local") is None

    def test_a_newer_release_passes(self, headless):
        desktop.require_newer(desktop.parse_feed(_feed("0.13.1"), channel="stable"), "0.13.0")

    def test_the_publishers_come_from_the_apps_updater_settings(self, tmp_path):
        res = tmp_path / "resources"
        (res / "backend-dist").mkdir(parents=True)
        (res / "app-update.yml").write_text(
            "provider: generic\npublisherName:\n  - Example Publisher\n", encoding="utf-8"
        )
        assert desktop.install_dir(res / "backend-dist") == tmp_path
        assert desktop.pinned_publishers(tmp_path) == PUBLISHERS

    def test_settings_that_pin_no_signer_are_refused(self, tmp_path):
        (tmp_path / "resources").mkdir()
        (tmp_path / "resources" / "app-update.yml").write_text("provider: generic\n")
        with pytest.raises(desktop.DesktopUpdateError) as err:
            desktop.pinned_publishers(tmp_path)
        assert err.value.code == desktop.ERR_SIGNER


# --- verification ----------------------------------------------------------------


class TestVerification:
    def _release(self, data: bytes = b"MZinstaller", **kw):
        return desktop.parse_feed(_feed(data=data, **kw), channel="stable")

    def _stage(self, release, data: bytes, signature=GOOD_SIGNATURE):
        async def download(_release, dest: Path) -> None:
            dest.write_bytes(data)

        return asyncio.run(
            desktop.stage(
                release,
                PUBLISHERS,
                download=download,
                signature_reader=lambda _p: dict(signature),
            )
        )

    def test_a_genuine_installer_is_staged(self, headless):
        path = self._stage(self._release(), b"MZinstaller")
        assert path.read_bytes() == b"MZinstaller"

    def test_the_install_directory_needs_the_apps_executable(self, tmp_path):
        with pytest.raises(desktop.DesktopUpdateError) as err:
            desktop.app_executable(tmp_path)
        assert err.value.code == desktop.ERR_INSTALL_DIR
        (tmp_path / "KiroCrew.exe").write_bytes(b"MZ")
        assert desktop.app_executable(tmp_path) == "KiroCrew.exe"

    def test_bytes_that_do_not_match_the_feed_are_refused_and_deleted(self, headless):
        release = self._release()
        with pytest.raises(desktop.DesktopUpdateError) as err:
            self._stage(release, b"MZinstallez")
        assert err.value.code == desktop.ERR_DIGEST
        assert not desktop.staged_path(release).exists()

    @pytest.mark.parametrize(
        ("signature", "code"),
        [
            ({"status": "NotSigned", "signer": "", "timestamped": False}, desktop.ERR_SIGNATURE),
            (
                {"status": "HashMismatch", "signer": "Example Publisher", "timestamped": True},
                desktop.ERR_SIGNATURE,
            ),
            (
                {"status": "Valid", "signer": "Example Publisher", "timestamped": False},
                desktop.ERR_SIGNATURE,
            ),
            (
                {"status": "Valid", "signer": "Someone Else", "timestamped": True},
                desktop.ERR_SIGNER,
            ),
        ],
    )
    def test_a_signature_that_is_not_the_pinned_publishers_is_refused(
        self, headless, signature, code
    ):
        release = self._release()
        with pytest.raises(desktop.DesktopUpdateError) as err:
            self._stage(release, b"MZinstaller", signature)
        assert err.value.code == code
        assert not desktop.staged_path(release).exists()

    def test_a_download_larger_than_the_feed_says_never_lands(self, headless):
        release = self._release()

        async def download(_release, dest: Path) -> None:
            raise desktop.DesktopUpdateError(desktop.ERR_DIGEST, "too large")

        with pytest.raises(desktop.DesktopUpdateError):
            asyncio.run(
                desktop.stage(
                    release,
                    PUBLISHERS,
                    download=download,
                    signature_reader=lambda _p: GOOD_SIGNATURE,
                )
            )
        assert list(desktop.staging_dir().glob("*")) == []

    def test_scope_follows_the_uninstall_hive(self, tmp_path):
        exe = f'"{tmp_path / "Uninstall KiroCrew.exe"}" /allusers'
        assert desktop.install_scope(tmp_path, entries=lambda: [("HKLM", "0.13.0", exe)]) == (
            desktop.SCOPE_ALL_USERS
        )

    def test_no_uninstall_entry_is_a_refusal(self, tmp_path):
        with pytest.raises(desktop.DesktopUpdateError) as err:
            desktop.install_scope(tmp_path, entries=lambda: [])
        assert err.value.code == desktop.ERR_SCOPE

    def test_an_all_users_install_needs_an_administrator(self):
        with pytest.raises(desktop.DesktopUpdateError) as err:
            desktop.require_rights(desktop.SCOPE_ALL_USERS, is_admin=lambda: False)
        assert err.value.code == desktop.ERR_ELEVATION
        desktop.require_rights(desktop.SCOPE_CURRENT_USER, is_admin=lambda: False)


# --- the helper --------------------------------------------------------------------


def _plan(**kw):
    base = dict(
        wait_pids=(4242,),
        installer="C:\\data\\desktop-update\\0.13.1-KiroCrew-Setup.exe",
        sha512=_sha512(b"MZinstaller"),
        size=11,
        publishers=PUBLISHERS,
        version="0.13.1",
        scope=desktop.SCOPE_ALL_USERS,
        install_dir="C:\\Program Files\\KiroCrew",
        result_path="C:\\data\\desktop_update_result.json",
        log_path="C:\\data\\desktop-update.log",
    )
    base.update(kw)
    return desktop.HelperPlan(**base)


class TestHelper:
    def test_every_value_is_a_single_quoted_literal(self):
        script = desktop.helper_script(_plan(install_dir="C:\\it's here"))
        assert "$InstallDir = 'C:\\it''s here'" in script
        assert "$WaitPids = @(4242)" in script
        assert "$Publishers = @('Example Publisher')" in script

    @pytest.mark.parametrize("quote", ["\u2018", "\u2019", "\u201a", "\u201b"])
    def test_a_typographic_quote_in_a_path_is_doubled(self, quote):
        # PowerShell ends a single-quoted string at these as well as at "'", so a
        # profile path like "Renee O\u2019Brien" must not end the literal early.
        home = f"C:\\Users\\Renee O{quote}Brien\\.kiro\\crew"
        script = desktop.helper_script(
            _plan(
                installer=f"{home}\\desktop-update\\0.13.1-KiroCrew-Setup.exe",
                result_path=f"{home}\\desktop_update_result.json",
                log_path=f"{home}\\desktop-update.log",
                restart_argv=(f"{home}\\python.exe", "-m", "kiro_crew", "gateway"),
                gateway_output=f"{home}\\out",
            )
        )
        doubled = f"C:\\Users\\Renee O{quote * 2}Brien\\.kiro\\crew"
        assert f"$ResultPath = '{doubled}\\desktop_update_result.json'" in script
        assert f"$LogPath = '{doubled}\\desktop-update.log'" in script
        assert f"'{doubled}\\python.exe'" in script
        assert f"O{quote}B" not in script.replace(quote * 2, "")

    def test_it_waits_then_reverifies_a_locked_copy_then_installs_then_checks(self):
        script = desktop.helper_script(_plan())
        order = [
            "WaitForExit($WaitSecs",
            "still runs from the install directory",
            "SetAccessRuleProtection",
            "ComputeHash",
            "Get-AuthenticodeSignature -LiteralPath $run",
            "TimeStamperCertificate",
            "$Publishers -notcontains $signer",
            "Join-Path $InstallDir $AppExe",
            '-ArgumentList \'/S\', "/$Scope", "/D=$dest" -PassThru',
            "$dir.Equals($InstallDir.TrimEnd",
            "$now -ne $Version",
            "Finish $true",
        ]
        positions = [script.index(step) for step in order]
        assert positions == sorted(positions)
        # Silent: no --updated (it turns /S into a visible run whose failure
        # prompts nobody can answer headless). /D= names the install directory
        # and is the last argument, as NSIS requires.
        assert "--updated" not in script
        assert script.index('"/D=$dest" -PassThru') > script.index("'/S', \"/$Scope\"")
        # Restart runs in `finally`, so a failed install still starts the old version,
        # but never while an installer that outlived its bound is still running.
        tail = script[script.index("} finally {") :]
        assert tail.index("-not $proc.HasExited") < tail.index("Restart-Gateway\n")
        assert "the gateway was left stopped" in tail

    def test_a_restart_argument_that_could_split_the_command_line_is_refused(self):
        ok = _plan(
            restart_argv=("C:\\Users\\José O'Brien\\python.exe", "-m", "kiro_crew", "gateway"),
            gateway_output="C:\\data\\out",
        )
        script = desktop.helper_script(ok)
        assert "'C:\\Users\\José O''Brien\\python.exe'" in script
        for bad in ('a"b', "a\nb", "a\x00b", "dir\\", ""):
            with pytest.raises(ValueError):
                desktop.helper_script(
                    _plan(restart_argv=("python.exe", bad), gateway_output="C:\\data\\out")
                )

    def test_an_unknown_app_executable_or_a_quoted_install_dir_is_refused(self):
        with pytest.raises(ValueError):
            desktop.helper_script(_plan(app_exe="evil.exe"))
        with pytest.raises(ValueError):
            desktop.helper_script(_plan(install_dir='C:\\a" /D=C:\\b'))

    def test_an_unknown_scope_is_refused(self):
        with pytest.raises(ValueError):
            desktop.helper_script(_plan(scope="/D=C:\\elsewhere"))

    def test_the_script_travels_encoded_not_as_a_file(self, monkeypatch):
        monkeypatch.setattr(desktop, "_powershell", lambda: "C:\\Windows\\System32\\powershell.exe")
        argv = desktop.helper_argv(_plan())
        encoded = argv[argv.index("-EncodedCommand") + 1]
        assert base64.b64decode(encoded).decode("utf-16-le") == desktop.helper_script(_plan())

    def test_a_failure_result_for_the_armed_version_is_read_back(self, headless):
        desktop._result_path().parent.mkdir(parents=True, exist_ok=True)
        desktop._result_path().write_text(
            '{"version": "0.13.1", "ok": false, "error": "the installer exited with 2"}',
            encoding="utf-8-sig",
        )
        assert desktop.result_error_for("0.13.1") == "the installer exited with 2"
        assert desktop.result_error_for("0.13.2") is None


# --- boot hand-off ---------------------------------------------------------------


class _Prepared:
    def __init__(self, version: str) -> None:
        self.release = type("R", (), {"version": version})()


@pytest.fixture
def boot(headless, monkeypatch):
    monkeypatch.setattr("kiro_crew.__version__", "0.13.0")
    monkeypatch.setattr(desktop, "headless_desktop_install", lambda dist=None: True)
    seen: dict[str, object] = {"handoffs": []}

    async def prepare(running, *, expected_version=None):
        seen["prepare"] = (running, expected_version)
        # The download binds nothing: a restart waiter must already see an apply.
        seen["applying_during_prepare"] = update_next_start.applying_started(os.getpid())
        if seen.get("refuse"):
            raise desktop.DesktopUpdateError(desktop.ERR_DIGEST, "refused for the test")
        return _Prepared(expected_version)

    def hand_off(prepared, *, wait_pids, restart):
        seen["handoffs"].append((prepared.release.version, wait_pids, restart))
        return 777

    monkeypatch.setattr(desktop, "prepare", prepare)
    monkeypatch.setattr(desktop, "hand_off", hand_off)
    return seen


class TestBootHandOff:
    def test_an_armed_update_is_verified_handed_off_and_the_start_ends(self, boot):
        update_next_start.arm("0.13.1")
        with pytest.raises(SystemExit) as stop:
            asyncio.run(update_boot_apply.apply_armed_update_at_boot())
        assert stop.value.code == 0
        assert boot["prepare"] == ("0.13.0", "0.13.1")
        assert boot["applying_during_prepare"] is not None
        ((version, pids, restart),) = boot["handoffs"]
        assert version == "0.13.1" and restart is True and len(pids) == 1
        assert update_next_start.handed_off(pids[0]) is True
        # The arm stays until the new version runs, so a failure can be reported.
        assert update_next_start.read().armed is True

    def test_a_refusal_keeps_the_old_install_and_reports_it(self, boot):
        boot["refuse"] = True
        update_next_start.arm("0.13.1")
        assert asyncio.run(update_boot_apply.apply_armed_update_at_boot()) is False
        assert boot["handoffs"] == []
        state = update_next_start.read()
        assert state.armed is False
        assert state.pending_apply_error == "refused for the test"
        assert update_next_start.applying_started(os.getpid()) is None

    def test_a_failure_the_helper_recorded_is_surfaced_once(self, boot):
        update_next_start.arm("0.13.1")
        desktop._result_path().write_text(
            '{"version": "0.13.1", "ok": false, "error": "the installer exited with 2"}'
        )
        assert asyncio.run(update_boot_apply.apply_armed_update_at_boot()) is False
        assert boot["handoffs"] == []
        assert update_next_start.read().pending_apply_error == "the installer exited with 2"
        assert desktop.read_result() is None

    def test_the_new_version_running_clears_the_arm(self, boot, monkeypatch):
        update_next_start.arm("0.13.1")
        update_next_start.mark_handed_off(4242)
        monkeypatch.setattr("kiro_crew.__version__", "0.13.1")
        assert asyncio.run(update_boot_apply.apply_armed_update_at_boot()) is False
        assert update_next_start.read().armed is False
        assert boot["handoffs"] == []
        assert not update_next_start.handed_off(4242)

    def test_a_memory_copy_that_did_not_land_installs_nothing(self, boot, monkeypatch):
        from kiro_crew.platform import wheel_apply

        monkeypatch.setattr(wheel_apply, "snapshot_memory_before_update", lambda: (0, "disk full"))
        update_next_start.arm("0.13.1")
        assert asyncio.run(update_boot_apply.apply_armed_update_at_boot()) is False
        assert boot["handoffs"] == []
        assert "Pre-update memory snapshot failed: disk full" in (
            update_next_start.read().pending_apply_error
        )

    def test_a_later_nightly_of_the_same_day_is_not_already_reached(self, boot, monkeypatch):
        monkeypatch.setattr("kiro_crew.__version__", "0.15.0.dev20261127090000")
        update_next_start.arm("0.15.0-nightly.20261127t180000")
        with pytest.raises(SystemExit):
            asyncio.run(update_boot_apply.apply_armed_update_at_boot())
        assert [h[0] for h in boot["handoffs"]] == ["0.15.0-nightly.20261127t180000"]

    def test_a_record_that_cannot_be_written_does_not_stop_the_start(self, boot, monkeypatch):
        """note_boot_attempt and finish both failing still starts the old version."""
        update_next_start.arm("0.13.1")

        def busy(*_a, **_kw):
            raise PermissionError("held without delete sharing")

        monkeypatch.setattr(update_next_start, "note_boot_attempt", busy)
        monkeypatch.setattr(update_next_start, "finish", busy)
        assert asyncio.run(update_boot_apply.apply_armed_update_at_boot()) is False
        assert boot["handoffs"] == []

    def test_a_policy_provider_keeps_its_own_apply(self, boot, monkeypatch):
        """A provider (or its refusal) decides; the desktop installer never stands in."""
        monkeypatch.setattr(desktop, "provider_owns_updates", lambda: True)
        ran: list[bool] = []

        async def child():
            ran.append(True)
            return False, "the policy refuses"

        monkeypatch.setattr(update_boot_apply, "_run_apply_child", child)
        monkeypatch.setattr(update_next_start, "installed_version", lambda: "0.13.0")
        update_next_start.arm("0.13.1")
        assert asyncio.run(update_boot_apply.apply_armed_update_at_boot()) is False
        assert ran == [True]
        assert "prepare" not in boot and boot["handoffs"] == []

    def test_a_desktop_hosted_gateway_leaves_the_apply_to_the_app(self, boot, monkeypatch):
        monkeypatch.setattr(desktop, "headless_desktop_install", lambda dist=None: False)
        monkeypatch.setenv(update_boot_apply.DESKTOP_HOSTED_ENV, "1")
        update_next_start.arm("0.13.1")
        assert asyncio.run(update_boot_apply.apply_armed_update_at_boot()) is False
        assert boot["handoffs"] == []
        assert update_next_start.read().armed is True

    def test_a_running_desktop_app_keeps_the_arm_for_a_start_without_it(self, boot, monkeypatch):
        """A gateway started beside the running app: the app's updater owns the install."""
        monkeypatch.setattr(desktop, "desktop_app_running", lambda root=None: True)
        update_next_start.arm("0.13.1")
        assert asyncio.run(update_boot_apply.apply_armed_update_at_boot()) is False
        assert "prepare" not in boot and boot["handoffs"] == []
        state = update_next_start.read()
        assert state.armed_version == "0.13.1" and not state.pending_apply_error


# --- restart waiter --------------------------------------------------------------


@pytest.mark.parametrize("apply_pid", [os.getpid, lambda: 4321])
def test_restart_ends_its_wait_when_the_replacement_handed_off(headless, apply_pid):
    """The helper waits for every process of the install, the restart CLI included.

    The record names the process that wrote it (here this one). A waiter holding
    a launcher shim's pid instead still recognises a record written after it began.
    """
    from kiro_crew import cli_server_restart as restart

    def exited():
        update_next_start.mark_handed_off(9999)
        return 0

    outcome, status, _ = restart._poll_until_serving(
        lambda: False, budget=60, extension=60, apply_pid=apply_pid, exited=exited
    )
    assert (outcome, status) == (restart._POLL_HANDED_OFF, None)


def test_a_download_through_a_launcher_shim_extends_the_wait(headless, monkeypatch):
    """The waiter holds the shim's pid; the gateway records its own before the download."""
    from kiro_crew import cli_server_restart as restart

    clock = iter(range(0, 10_000, 30))
    # The waiter's own `time` binding, never the shared stdlib module's attributes.
    monkeypatch.setattr(
        restart,
        "time",
        types.SimpleNamespace(
            monotonic=lambda: float(next(clock)), sleep=lambda _s: None, time=time.time
        ),
    )
    monkeypatch.setattr(restart, "_print_applying_update", lambda _s: None)
    monkeypatch.setattr(restart, "_print_still_starting", lambda _s: None)
    answers = iter([False, False, False, False, True])

    def serving():
        update_next_start.mark_applying()
        return next(answers)

    outcome, _status, _ = restart._poll_until_serving(
        serving, budget=60, extension=60, apply_pid=lambda: 4321, exited=lambda: None
    )
    assert outcome == restart._POLL_SERVED


def test_a_hand_off_from_before_the_restart_is_not_this_ones(headless, monkeypatch):
    from kiro_crew import cli_server_restart as restart

    # A controlled clock: the record is stamped strictly before the wait starts,
    # so a coarse wall clock (one tick for both reads) cannot make them equal.
    monkeypatch.setattr(update_next_start, "wall_clock", lambda: 1_000.0)
    path = update_next_start._applying_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 9999, "started": 999.0, "helper_pid": 1}), encoding="utf-8")
    monkeypatch.setattr(update_next_start, "time", types.SimpleNamespace(time=lambda: 1_000.0))
    outcome, status, _ = restart._poll_until_serving(
        lambda: False, budget=60, extension=60, apply_pid=lambda: 4321, exited=lambda: 1
    )
    assert (outcome, status) == (restart._POLL_EXITED, 1)


def test_restart_still_reports_a_replacement_that_died(headless, monkeypatch):
    from kiro_crew import cli_server_restart as restart

    outcome, status, _ = restart._poll_until_serving(
        lambda: False,
        budget=60,
        extension=60,
        apply_pid=lambda: 9999,
        exited=lambda: 1,
    )
    assert (outcome, status) == (restart._POLL_EXITED, 1)


# --- CLI -------------------------------------------------------------------------


class TestCli:
    @pytest.fixture
    def cli(self, boot, monkeypatch):
        from kiro_crew import cli_desktop_update

        monkeypatch.setattr(cli_desktop_update, "_gateway_running", lambda: False)

        async def prepare(running, *, expected_version=None):
            boot["prepare"] = (running, expected_version)
            if boot.get("refuse"):
                raise desktop.DesktopUpdateError(*boot["refuse"])
            prepared = _Prepared(expected_version or "0.13.1")
            prepared.publishers = PUBLISHERS
            return prepared

        monkeypatch.setattr(desktop, "prepare", prepare)
        return cli_desktop_update

    def test_a_refusal_exits_non_zero_and_installs_nothing(self, cli, boot, capsys):
        boot["refuse"] = (desktop.ERR_SIGNER, "not signed by the pinned publisher")
        with pytest.raises(SystemExit) as stop:
            cli.update_desktop_install()
        assert stop.value.code == 1
        assert boot["handoffs"] == []
        assert "not signed by the pinned publisher" in capsys.readouterr().out

    def test_nothing_newer_is_up_to_date_and_exits_zero(self, cli, boot, capsys):
        boot["refuse"] = (desktop.ERR_DOWNGRADE, "not newer")
        cli.update_desktop_install()
        assert boot["handoffs"] == []
        assert "Already on the latest version" in capsys.readouterr().out

    def test_with_a_gateway_running_it_arms_for_the_next_restart(self, cli, boot, monkeypatch):
        monkeypatch.setattr(cli, "_gateway_running", lambda: True)
        cli.update_desktop_install()
        assert boot["handoffs"] == []
        assert update_next_start.read().armed_version == "0.13.1"

    def test_with_no_gateway_it_installs_now_and_starts_nothing(self, cli, boot):
        cli.update_desktop_install()
        ((version, _pids, restart),) = boot["handoffs"]
        assert (version, restart) == ("0.13.1", False)
        assert update_next_start.read().armed_version == "0.13.1"

    def test_stop_installs_an_armed_update_and_leaves_the_gateway_stopped(self, cli, boot):
        update_next_start.arm("0.13.1")
        cli.apply_after_stop()
        assert boot["prepare"] == ("0.13.0", "0.13.1")
        ((version, _pids, restart),) = boot["handoffs"]
        assert (version, restart) == ("0.13.1", False)

    def test_stop_with_nothing_armed_does_nothing(self, cli, boot):
        cli.apply_after_stop()
        assert "prepare" not in boot and boot["handoffs"] == []

    def test_no_memory_copy_means_no_install_and_a_non_zero_exit(self, cli, boot, monkeypatch):
        from kiro_crew.platform import wheel_apply

        monkeypatch.setattr(wheel_apply, "snapshot_memory_before_update", lambda: (0, "disk full"))
        with pytest.raises(SystemExit) as stop:
            cli.update_desktop_install()
        assert stop.value.code == 1
        assert boot["handoffs"] == []
        update_next_start.arm("0.13.1")
        cli.apply_after_stop()
        assert boot["handoffs"] == []
        assert "disk full" in update_next_start.read().pending_apply_error

    def test_stop_where_a_policy_provider_owns_updates_does_nothing(self, cli, boot, monkeypatch):
        monkeypatch.setattr(desktop, "provider_owns_updates", lambda: True)
        update_next_start.arm("0.13.1")
        cli.apply_after_stop()
        assert "prepare" not in boot and boot["handoffs"] == []

    def test_an_externally_managed_install_is_never_updated_from_the_cli(
        self, cli, boot, monkeypatch, capsys
    ):
        """`kirocrew update` and `kirocrew stop` defer to the operator who owns updates."""
        from kiro_crew import cli_server_update

        monkeypatch.setattr(desktop, "headless_desktop_install", _HEADLESS_DESKTOP_INSTALL)
        monkeypatch.setattr(desktop, "externally_managed", lambda root=None: True)
        monkeypatch.setattr(desktop, "provider_owns_updates", lambda: False)
        monkeypatch.setattr("kiro_crew.cli_server.distribution", lambda: "nsis")
        monkeypatch.setattr("kiro_crew.beacon.distribution", lambda: "nsis")
        monkeypatch.setattr(
            "kiro_crew.platform.update_provider.resolve_provider", lambda *_a, **_k: None
        )
        monkeypatch.setattr(
            "kiro_crew.cli_server.derive_capability",
            lambda install_root="": derive_capability(install_root="", dist="nsis"),
        )
        cli_server_update._update()
        assert "managed externally" in capsys.readouterr().out
        update_next_start.arm("0.13.1")
        cli.apply_after_stop()
        assert "prepare" not in boot and boot["handoffs"] == []
        assert update_next_start.read().armed_version == "0.13.1"

    def test_stop_on_another_install_does_nothing(self, cli, boot, monkeypatch):
        monkeypatch.setattr(desktop, "headless_desktop_install", lambda dist=None: False)
        update_next_start.arm("0.13.1")
        cli.apply_after_stop()
        assert boot["handoffs"] == []

    def test_update_while_the_desktop_app_hosts_the_gateway_defers_to_it(
        self, cli, boot, monkeypatch, capsys
    ):
        """A terminal's `kirocrew update` never carries the app's variable: the host decides."""
        monkeypatch.setattr(cli, "_gateway_running", lambda: True)
        monkeypatch.setattr(desktop, "desktop_app_running", lambda root=None: True)
        cli.update_desktop_install()
        assert "prepare" not in boot and boot["handoffs"] == []
        assert update_next_start.read().armed is False
        out = capsys.readouterr().out
        assert "desktop app is running from this install" in out
        assert "desktop app's built-in updater" in out

    def test_stop_of_an_app_hosted_gateway_leaves_the_apps_arm(
        self, cli, boot, monkeypatch, capsys
    ):
        monkeypatch.setattr(desktop, "desktop_app_running", lambda root=None: True)
        update_next_start.arm("0.13.1")
        cli.apply_after_stop()
        assert "prepare" not in boot and boot["handoffs"] == []
        state = update_next_start.read()
        assert state.armed_version == "0.13.1" and not state.pending_apply_error
        assert "desktop app is running from this install" in capsys.readouterr().out
