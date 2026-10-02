"""Every gateway setting rides an export or a snapshot, and a restore says what it did.

The user-facing promise: export from one install, import into a new one, and every
choice on the dashboard Settings page comes back. Before this, three things broke it:

* the dashboard's default Merge import copied ``config.json`` only where the destination
  had none -- and every running install has one (the gateway writes defaults at boot), so
  no setting was ever restored while the summary reported success;
* neither the export nor the snapshot ``config`` component carried
  ``config.local.json``, ``ui-prefs.json`` or ``notification_settings.json``;
* ``kirocrew restore --mode merge`` printed a bare "✅ config" over a merge that kept
  every one of the destination's files.

The dashboard Merge keeps main's never-overwrite rule, made honest: a settings
document this install lacks is installed from the archive, one it has is kept
untouched and NAMED (``settings_kept``), and the ``config.local.json`` overlay is
never installed by a Merge at all. Replace is the path that restores the archive's
settings over this install's.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from test_snapshot import _make_snapshot, _setup_fake_kirocrew, unpinnable_argv

from kiro_crew import portability
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.notifications.settings import ChannelSettings
from kiro_crew.snapshot import restore_main
from kiro_crew.snapshot_components import COMPONENT_JSON_OBJECTS, CORE_FILES

_SETTINGS_FILES = ("config.local.json", "ui-prefs.json", "notification_settings.json")
_ALL_FOUR = ("config.json", *_SETTINGS_FILES)


def _source_home(root: Path) -> Path:
    """An install whose user chose settings in every settings document."""
    home = root / "source"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "timezone": "Asia/Tokyo",
                "dashboard": {"theme_color": "emerald"},
                "agents": {"reviewer": {"kiro_agent": "kirocrew", "workspace": "default"}},
                "meta": {"written_by": "the source install"},
            }
        ),
        encoding="utf-8",
    )
    (home / "config.local.json").write_text(
        json.dumps({"dashboard": {"language": "ja"}}), encoding="utf-8"
    )
    (home / "ui-prefs.json").write_text(
        json.dumps(
            {
                "prefs": {
                    "mc-chat-config": '{"sendKey":"enter"}',
                    "mc-terminal-font": '{"fontSize":15}',
                    # Hand-planted: no client can store it, so no import may either.
                    "kiro_crew_token": "bearer-abc",
                }
            }
        ),
        encoding="utf-8",
    )
    (home / "notification_settings.json").write_text(
        json.dumps(
            {
                "channel_settings": {
                    "system.heartbeat": {"muted": True},
                    "system.monitor": {},
                    # Protected: an archive cannot mute approvals either.
                    "system.approval": {"muted": True},
                }
            }
        ),
        encoding="utf-8",
    )
    return home


def _export(home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    data, _manifest = portability.create_export_zip()
    out = tmp_path / "export.zip"
    out.write_bytes(data)
    return out


def _fresh_install(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home seeded the way a just-installed gateway leaves it: a full default config."""
    home = root / "target"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    KiroCrewConfig().save()
    doc = json.loads((home / "config.json").read_text(encoding="utf-8"))
    doc.setdefault("dashboard", {})["destination_only"] = "kept"
    doc["agents"] = {"default": {"kiro_agent": "kirocrew", "workspace": "default"}}
    (home / "config.json").write_text(json.dumps(doc), encoding="utf-8")
    return home


def _with_member(zip_path: Path, name: str, payload: bytes) -> None:
    """Rewrite *zip_path* with its ``<root>/<name>`` member replaced by *payload*."""
    with zipfile.ZipFile(zip_path) as zf:
        members = {i.filename: zf.read(i) for i in zf.infolist()}
    root = next(iter(members)).split("/", 1)[0]
    members[f"{root}/{name}"] = payload
    with zipfile.ZipFile(zip_path, "w") as zf:
        for member, data in members.items():
            zf.writestr(member, data)


class TestTheExportCarriesEverySettingsDocument:
    def test_all_four_documents_ride_the_dashboard_export(self, tmp_path, monkeypatch):
        home = _source_home(tmp_path)
        zip_path = _export(home, monkeypatch, tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = {n.split("/", 1)[1] for n in zf.namelist()}
        for name in _ALL_FOUR:
            assert name in names

    def test_an_absent_document_is_simply_not_exported(self, tmp_path, monkeypatch):
        home = tmp_path / "bare"
        home.mkdir()
        (home / "config.json").write_text("{}", encoding="utf-8")
        zip_path = _export(home, monkeypatch, tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = {n.split("/", 1)[1] for n in zf.namelist()}
        assert not names & set(_SETTINGS_FILES)


def _empty_home(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = root / "empty"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


class TestMergeNeverOverwritesASettingsDocument:
    def test_an_installs_config_is_kept_byte_identical_and_named(self, tmp_path, monkeypatch):
        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        target = _fresh_install(tmp_path, monkeypatch)
        before = (target / "config.json").read_bytes()

        summary = portability.apply_import_zip(zip_path, mode="merge")

        assert (target / "config.json").read_bytes() == before
        assert (
            "config (kept this install's; import with Replace to restore the archive's)"
            in summary["items"]
        )
        assert "config.json" in summary["settings_kept"]
        assert not summary.get("refused_merges")

    def test_the_documents_an_install_lacks_are_restored(self, tmp_path, monkeypatch):
        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        target = _empty_home(tmp_path, monkeypatch)
        live = ChannelSettings()

        summary = portability.apply_import_zip(zip_path, mode="merge", channel_settings=live)

        config = json.loads((target / "config.json").read_text(encoding="utf-8"))
        assert config["timezone"] == "Asia/Tokyo"
        assert config["dashboard"]["theme_color"] == "emerald"
        # The source's meta block is not adopted; this install's writer stamps its own.
        assert config.get("meta", {}).get("written_by") != "the source install"
        assert "config (restored)" in summary["items"]

        prefs = json.loads((target / "ui-prefs.json").read_text(encoding="utf-8"))["prefs"]
        assert prefs == {
            "mc-chat-config": '{"sendKey":"enter"}',
            "mc-terminal-font": '{"fontSize":15}',
        }
        assert summary["ui_prefs_restored"] is True
        assert "ui-prefs (restored; 1 unstorable entry dropped)" in summary["items"]

        # Through the running store: the restored mute applies at once.
        assert live.get("system.heartbeat") == {"muted": True}
        assert live.get("system.approval") == {}  # protected: the archive cannot mute it
        on_disk = json.loads((target / "notification_settings.json").read_text())
        assert on_disk["channel_settings"]["system.heartbeat"] == {"muted": True}
        assert "notification-settings (restored; 1 invalid value dropped)" in summary["items"]

        # Only the overlay was left out, and it is named.
        assert summary["settings_kept"] == ["config.local.json"]
        assert not summary.get("refused_merges")

    def test_the_overlay_is_never_installed_even_where_absent(self, tmp_path, monkeypatch):
        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        target = _empty_home(tmp_path, monkeypatch)

        summary = portability.apply_import_zip(zip_path, mode="merge")

        assert not (target / "config.local.json").exists()
        assert "config.local.json" in summary["settings_kept"]
        assert any(i.startswith("config.local (not installed:") for i in summary["items"])

    def test_existing_documents_are_kept_untouched(self, tmp_path, monkeypatch):
        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        target = _fresh_install(tmp_path, monkeypatch)
        (target / "config.local.json").write_text('{"dashboard": {"language": "de"}}')
        (target / "ui-prefs.json").write_text('{"prefs": {"mc-chat-config": "local"}}')
        live = ChannelSettings()
        live.update("oncall.page", priority="critical")
        before = {n: (target / n).read_bytes() for n in _ALL_FOUR}

        summary = portability.apply_import_zip(zip_path, mode="merge", channel_settings=live)

        assert {n: (target / n).read_bytes() for n in _ALL_FOUR} == before
        assert summary["settings_kept"] == list(_ALL_FOUR)
        assert "ui_prefs_restored" not in summary
        assert live.get("system.heartbeat") == {}
        assert live.get("oncall.page") == {"priority": "critical"}
        for label in ("config", "config.local", "ui-prefs", "notification-settings"):
            assert (
                f"{label} (kept this install's; import with Replace to restore the archive's)"
                in summary["items"]
            )

    def test_a_second_merge_does_not_ask_the_client_to_reload(self, tmp_path, monkeypatch):
        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        target = _fresh_install(tmp_path, monkeypatch)
        first = portability.apply_import_zip(zip_path, mode="merge")
        assert first["ui_prefs_restored"] is True

        again = portability.apply_import_zip(zip_path, mode="merge")

        assert "ui_prefs_restored" not in again
        assert "ui-prefs.json" in again["settings_kept"]
        assert json.loads((target / "ui-prefs.json").read_text())["prefs"]


class TestAMalformedArchiveDocumentIsRefused:
    def test_a_non_object_archive_config_is_refused(self, tmp_path, monkeypatch):
        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        _with_member(zip_path, "config.json", b'["not", "a", "config"]')
        target = _empty_home(tmp_path, monkeypatch)

        summary = portability.apply_import_zip(zip_path, mode="merge")

        assert not (target / "config.json").exists()
        skipped = [i for i in summary["items"] if i.startswith("config (skipped:")]
        assert skipped and "not an object" in skipped[0]
        assert "config" in summary["refused_merges"]
        assert "config.json" not in summary.get("settings_kept", [])

    def test_a_malformed_ui_prefs_is_reported_and_never_installed(self, tmp_path, monkeypatch):
        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        _with_member(zip_path, "ui-prefs.json", b'{"prefs": ["x"]}')
        target = _fresh_install(tmp_path, monkeypatch)

        summary = portability.apply_import_zip(zip_path, mode="merge")

        assert not (target / "ui-prefs.json").exists()
        assert "ui-prefs" in summary["refused_merges"]
        assert "ui_prefs_restored" not in summary

    def test_a_malformed_notification_settings_is_refused(self, tmp_path, monkeypatch):
        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        _with_member(zip_path, "notification_settings.json", b'{"channel_settings": []}')
        target = _fresh_install(tmp_path, monkeypatch)

        summary = portability.apply_import_zip(zip_path, mode="merge")

        assert not (target / "notification_settings.json").exists()
        assert "notification-settings" in summary["refused_merges"]
        assert any(i.startswith("notification-settings (skipped:") for i in summary["items"])

    @pytest.mark.parametrize("name", ["config.json", "notification_settings.json"])
    def test_an_oversized_document_is_refused_before_it_is_parsed(
        self, tmp_path, monkeypatch, name
    ):
        """The archive may hold 2 GiB; a settings document past the cap is refused
        without reading it whole, so it cannot exhaust the gateway's memory."""
        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        cap = portability._MAX_SETTINGS_DOCUMENT_BYTES
        _with_member(zip_path, name, b'{"x": "' + b"a" * cap + b'"}')
        _empty_home(tmp_path, monkeypatch)
        parsed: list[int] = []
        real_loads = json.loads
        monkeypatch.setattr(
            portability.json,
            "loads",
            lambda t, *a, **k: parsed.append(len(t)) or real_loads(t, *a, **k),
        )

        summary = portability.apply_import_zip(zip_path, mode="merge")

        assert all(n <= cap for n in parsed)
        assert any("larger than" in i for i in summary["items"])

    def test_replace_leaves_the_live_copy_of_a_refused_document(self, tmp_path, monkeypatch):
        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        _with_member(zip_path, "config.local.json", b"42")
        target = _fresh_install(tmp_path, monkeypatch)
        (target / "config.local.json").write_text('{"dashboard": {"language": "de"}}')

        summary = portability.apply_import_zip(zip_path, mode="replace")

        assert json.loads((target / "config.local.json").read_text()) == {
            "dashboard": {"language": "de"}
        }
        assert "config.local" in summary["refused_merges"]

    @pytest.mark.parametrize("name", ["config.json", "config.local.json"])
    def test_a_config_nested_past_the_readers_depth_is_refused(self, tmp_path, monkeypatch, name):
        """It parses, but the config cache's deepcopy would overflow on every later load."""
        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        deep = '{"x": ' + '{"a": ' * 600 + "1" + "}" * 600 + "}"
        _with_member(zip_path, name, deep.encode())
        target = _fresh_install(tmp_path, monkeypatch)
        live = '{"dashboard": {"language": "de"}}'
        (target / name).write_text(live)

        summary = portability.apply_import_zip(zip_path, mode="replace")

        assert (target / name).read_text() == live
        label = name.removesuffix(".json")
        assert label in summary["refused_merges"]
        skipped = [i for i in summary["items"] if i.startswith(f"{label} (skipped:")]
        assert skipped and "nests deeper than" in skipped[0]


class TestReplaceRestoresEverySettingsDocument:
    def test_the_swap_holds_both_settings_stores_writer_locks(self, tmp_path, monkeypatch):
        """A write from another tab or channel cannot land mid-swap and publish its
        pre-import copy over the restored file."""
        from kiro_crew import ui_prefs
        from kiro_crew.notifications import settings as notification_settings

        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        _fresh_install(tmp_path, monkeypatch)
        held: list[tuple[bool, bool]] = []
        real = portability._do_replace

        def _spy(*a, **k):
            held.append((ui_prefs._write_lock.locked(), notification_settings._lock.locked()))
            return real(*a, **k)

        monkeypatch.setattr(portability, "_do_replace", _spy)
        portability.apply_import_zip(zip_path, mode="replace", channel_settings=ChannelSettings())
        assert held == [(True, True)]

    def test_replace_restores_all_four_and_the_users_settings_land(self, tmp_path, monkeypatch):
        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        target = _fresh_install(tmp_path, monkeypatch)
        (target / "ui-prefs.json").write_text('{"prefs": {"mc-chat-config": "local"}}')
        live = ChannelSettings()
        live.update("system.heartbeat", muted=False)
        old_config = (target / "config.json").read_bytes()

        summary = portability.apply_import_zip(zip_path, mode="replace", channel_settings=live)

        config = json.loads((target / "config.json").read_text(encoding="utf-8"))
        assert config["timezone"] == "Asia/Tokyo"
        assert config["dashboard"]["theme_color"] == "emerald"
        assert json.loads((target / "config.local.json").read_text()) == {
            "dashboard": {"language": "ja"}
        }
        prefs = json.loads((target / "ui-prefs.json").read_text(encoding="utf-8"))["prefs"]
        assert prefs["mc-chat-config"] == '{"sendKey":"enter"}'
        assert "kiro_crew_token" not in prefs
        assert summary["ui_prefs_restored"] is True
        # The running store re-read the swapped file.
        assert live.get("system.heartbeat") == {"muted": True}
        assert live.get("system.approval") == {}
        # And the settings load as the user chose them.
        from kiro_crew.config.loader import _invalidate_config_cache

        _invalidate_config_cache()
        loaded = KiroCrewConfig.load()
        assert loaded.timezone == "Asia/Tokyo"
        assert loaded.dashboard.theme_color == "emerald"
        # Backed up first: the replaced config is in the pre-restore directory.
        backups = [p for p in target.glob("pre-restore-*/config.json")]
        assert any(p.read_bytes() == old_config for p in backups)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_replace_installs_the_settings_documents_owner_only(self, tmp_path, monkeypatch):
        """Replace must not install a document wider than its own live writer writes.

        The zip extractor stages every member at the umask default (zipfile does
        not restore modes), and the replace copy propagates the staged mode -- so
        without the staging lockdown ui-prefs.json landed group/world-readable
        while `ui_prefs`'s writer always uses 0o600, and the config documents
        landed wider than the config writer's own 0o600 create mode.
        notification_settings.json keeps the umask its live writer uses.
        """
        zip_path = _export(_source_home(tmp_path), monkeypatch, tmp_path)
        target = _fresh_install(tmp_path, monkeypatch)

        portability.apply_import_zip(zip_path, mode="replace", channel_settings=ChannelSettings())

        for name in ("config.json", "config.local.json", "ui-prefs.json"):
            mode = stat.S_IMODE((target / name).stat().st_mode)
            assert mode == 0o600, (name, oct(mode))


class TestTheSnapshotConfigComponent:
    def test_it_carries_every_settings_document(self):
        for name in _SETTINGS_FILES:
            assert name in CORE_FILES["config"]
            # Each consumer reads an object and degrades to empty on anything else.
            assert name in COMPONENT_JSON_OBJECTS

    def test_a_snapshot_stages_and_a_replace_restores_them(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        _setup_fake_kirocrew(src)
        (src / "config.local.json").write_text('{"dashboard": {"language": "ja"}}')
        (src / "ui-prefs.json").write_text('{"prefs": {"mc-nav": "x"}}')
        (src / "notification_settings.json").write_text(
            '{"channel_settings": {"system.heartbeat": {"muted": true}}}'
        )
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        tarball = _make_snapshot(src, tmp_path / "out")
        with tarfile.open(tarball) as tar:
            names = {Path(m.name).name for m in tar.getmembers()}
        assert set(_SETTINGS_FILES) <= names

        dst = tmp_path / "dst"
        _setup_fake_kirocrew(dst)
        (dst / "ui-prefs.json").write_text('{"prefs": {"mc-nav": "old"}}')
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main(
            [str(tarball), "--mode", "replace", "--components", "config", "--force"]
            + unpinnable_argv()
        )
        assert ret == 0
        assert json.loads((dst / "ui-prefs.json").read_text())["prefs"] == {"mc-nav": "x"}
        assert (dst / "config.local.json").is_file()
        assert (dst / "notification_settings.json").is_file()


class TestCliRestoreInstallsSettingsOwnerOnly:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits; Windows uses a DACL")
    @pytest.mark.parametrize("mode", ["replace", "merge"])
    def test_a_restored_settings_document_is_owner_only(self, tmp_path, monkeypatch, mode):
        """The overlay can hold channel tokens; a restored copy must not keep a wider
        mode than the writer that creates it."""
        src = tmp_path / "src"
        _setup_fake_kirocrew(src)
        (src / "config.local.json").write_text('{"telegram": {"bot_token": "t"}}')
        (src / "ui-prefs.json").write_text('{"prefs": {"mc-nav": "x"}}')
        for name in ("config.json", "config.local.json", "ui-prefs.json"):
            os.chmod(src / name, 0o644)
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        tarball = _make_snapshot(src, tmp_path / "out")
        dst = tmp_path / "dst"
        _setup_fake_kirocrew(dst)
        for name in ("config.json", "config.local.json", "ui-prefs.json"):
            (dst / name).unlink(missing_ok=True)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))

        ret = restore_main(
            [str(tarball), "--mode", mode, "--components", "config", "--force"] + unpinnable_argv()
        )

        assert ret == 0
        names = (
            ("config.json", "config.local.json", "ui-prefs.json")
            if mode == "replace"
            else (
                "config.json",
                "ui-prefs.json",
            )
        )
        for name in names:
            assert stat.S_IMODE((dst / name).stat().st_mode) == 0o600, name


class TestCliRestoreRefusesAnUnusableSettingsDocument:
    @pytest.mark.parametrize(
        ("name", "body"),
        [
            ("ui-prefs.json", '{"prefs": []}'),
            ("ui-prefs.json", json.dumps({"prefs": {f"mc-k{i}": "v" for i in range(201)}})),
            ("notification_settings.json", '{"channel_settings": []}'),
            (
                "ui-prefs.json",
                json.dumps(
                    {"prefs": {**{f"mc-k{i}": "v" for i in range(200)}, "mc-" + "x" * 200: "v"}}
                ),
            ),
            ("notification_settings.json", '{"channel_settings": {"app.a": {"muted": "false"}}}'),
            ("ui-prefs.json", '{"prefs": {"mc-x": ' + "[" * 100_000 + "]" * 100_000 + "}}"),
            ("config.local.json", '{"x": ' + '{"a": ' * 600 + "1" + "}" * 600 + "}"),
        ],
        ids=[
            "prefs-not-an-object",
            "prefs-over-the-key-limit",
            "channel-settings-not-an-object",
            "prefs-with-an-entry-the-store-drops",
            "channel-settings-with-an-entry-the-store-drops",
            "prefs-nested-past-the-parser-limit",
            "config-overlay-nested-past-the-reader-limit",
        ],
    )
    @pytest.mark.parametrize("mode", ["replace", "merge"])
    def test_it_is_refused_and_the_live_copy_is_kept(self, tmp_path, monkeypatch, name, body, mode):
        """Each passes the generic "a JSON object" check, then reads back as no saved
        settings (or makes every later prefs save fail), so it must not be installed."""
        src = tmp_path / "src"
        _setup_fake_kirocrew(src)
        (src / name).write_text(body)
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        tarball = _make_snapshot(src, tmp_path / "out")
        dst = tmp_path / "dst"
        _setup_fake_kirocrew(dst)
        live = dst / name
        if mode == "replace":
            live.write_text(
                '{"prefs": {"mc-nav": "old"}}'
                if name == "ui-prefs.json"
                else '{"channel_settings": {"app.a": {"muted": true}}}'
            )
        else:
            live.unlink(missing_ok=True)  # merge installs only where the file is absent
        before = live.read_bytes() if live.exists() else None
        monkeypatch.setenv("KIROCREW_HOME", str(dst))

        ret = restore_main(
            [str(tarball), "--mode", mode, "--components", "config", "--force"] + unpinnable_argv()
        )

        assert ret != 0
        assert (live.read_bytes() if live.exists() else None) == before


class TestCliMergeSaysWhatItKept:
    def _bundle(self, tmp_path, monkeypatch) -> Path:
        src = tmp_path / "src"
        _setup_fake_kirocrew(src)
        (src / "config.json").write_text('{"timezone": "Asia/Tokyo"}')
        (src / "ui-prefs.json").write_text('{"prefs": {"mc-nav": "x"}}')
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        return _make_snapshot(src, tmp_path / "out")

    def test_a_kept_config_is_named_instead_of_ticked(self, tmp_path, monkeypatch, capsys):
        tarball = self._bundle(tmp_path, monkeypatch)
        dst = tmp_path / "dst"
        _setup_fake_kirocrew(dst)
        (dst / "config.json").write_text('{"timezone": "UTC"}')
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        capsys.readouterr()

        ret = restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())

        out = capsys.readouterr().out
        assert ret == 0
        # Merge never overwrites -- that contract is unchanged.
        assert json.loads((dst / "config.json").read_text())["timezone"] == "UTC"
        assert "config.json: kept the existing file" in out
        assert "--mode replace --components config" in out
        assert "✅ config" not in out
        # A file the destination lacked is still restored, and said so.
        assert "ui-prefs.json: restored (was missing)" in out
        assert json.loads((dst / "ui-prefs.json").read_text())["prefs"] == {"mc-nav": "x"}

    def test_a_bundle_overlay_is_never_installed_by_a_merge(self, tmp_path, monkeypatch, capsys):
        src = tmp_path / "src"
        _setup_fake_kirocrew(src)
        (src / "config.json").write_text('{"timezone": "Asia/Tokyo"}')
        # The overlay outranks config.json at load: raw, it would turn the sandbox off.
        (src / "config.local.json").write_text('{"agent": {"sandbox": "off"}}')
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        tarball = _make_snapshot(src, tmp_path / "out")
        dst = tmp_path / "dst"
        _setup_fake_kirocrew(dst)
        (dst / "config.local.json").unlink(missing_ok=True)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        capsys.readouterr()

        ret = restore_main(
            [str(tarball), "--mode", "merge", "--components", "config", "--force"]
            + unpinnable_argv()
        )

        out = capsys.readouterr().out
        assert ret == 0
        assert not (dst / "config.local.json").exists()
        assert "config.local.json: not applied" in out
        assert "--mode replace --components config" in out
        assert "✅ config" not in out

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
    def test_a_dangling_link_at_a_missing_settings_file_is_never_followed(
        self, tmp_path, monkeypatch, capsys
    ):
        """`is_file()` is false for a dangling link; copying through it would write the
        bundle's file wherever an agent pointed the link, outside the data home."""
        src = tmp_path / "src"
        _setup_fake_kirocrew(src)
        (src / "ui-prefs.json").write_text('{"prefs": {"mc-nav": "x"}}')
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        tarball = _make_snapshot(src, tmp_path / "out")
        dst = tmp_path / "dst"
        _setup_fake_kirocrew(dst)
        (dst / "ui-prefs.json").unlink(missing_ok=True)
        outside = tmp_path / "outside.json"
        os.symlink(outside, dst / "ui-prefs.json")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        capsys.readouterr()

        restore_main(
            [str(tarball), "--mode", "merge", "--components", "config", "--force"]
            + unpinnable_argv()
        )

        assert not outside.exists()
        assert (dst / "ui-prefs.json").is_symlink()
        assert "ui-prefs.json: restored" not in capsys.readouterr().out

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
    def test_a_linked_settings_file_is_refused_before_its_target_is_read(
        self, tmp_path, monkeypatch, capsys
    ):
        """Comparing contents first would read the link's target -- a credential file
        outside the data home -- and its match/mismatch would show in the output."""
        import filecmp

        body = '{"prefs": {"mc-nav": "x"}}'
        src = tmp_path / "src"
        _setup_fake_kirocrew(src)
        (src / "ui-prefs.json").write_text(body)
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        tarball = _make_snapshot(src, tmp_path / "out")
        dst = tmp_path / "dst"
        _setup_fake_kirocrew(dst)
        (dst / "ui-prefs.json").unlink(missing_ok=True)
        outside = tmp_path / "credential.json"
        outside.write_text(body)
        os.symlink(outside, dst / "ui-prefs.json")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        real_cmp = filecmp.cmp
        compared: list[str] = []

        def _cmp(a, b, *args, **kwargs):
            compared.append(str(b))
            return real_cmp(a, b, *args, **kwargs)

        monkeypatch.setattr(filecmp, "cmp", _cmp)
        capsys.readouterr()

        restore_main(
            [str(tarball), "--mode", "merge", "--components", "config", "--force"]
            + unpinnable_argv()
        )

        assert not any(c.endswith("ui-prefs.json") for c in compared)
        assert "ui-prefs.json: not restored; the existing entry is not a regular file" in (
            capsys.readouterr().out
        )
        assert outside.read_text() == body

    def test_identical_files_still_earn_the_tick(self, tmp_path, monkeypatch, capsys):
        tarball = self._bundle(tmp_path, monkeypatch)
        dst = tmp_path / "dst"
        _setup_fake_kirocrew(dst)
        # The bundle's own bytes: staging may have rewritten the source file (a load-time
        # migration stamps it), so a hand-written copy is not "the same file".
        with tarfile.open(tarball) as tar:
            for member in tar.getmembers():
                name = Path(member.name).name
                if member.isfile() and name in CORE_FILES["config"]:
                    extracted = tar.extractfile(member)
                    assert extracted is not None
                    (dst / name).write_bytes(extracted.read())
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        capsys.readouterr()

        ret = restore_main(
            [str(tarball), "--mode", "merge", "--components", "config", "--force"]
            + unpinnable_argv()
        )

        out = capsys.readouterr().out
        assert ret == 0
        assert "kept the existing file" not in out
        assert "✅ config" in out

    def test_host_runtime_state_is_not_reported_as_kept_settings(
        self, tmp_path, monkeypatch, capsys
    ):
        tarball = self._bundle(tmp_path, monkeypatch)
        dst = tmp_path / "dst"
        _setup_fake_kirocrew(dst)
        with tarfile.open(tarball) as tar:
            for member in tar.getmembers():
                name = Path(member.name).name
                if member.isfile() and name in CORE_FILES["config"]:
                    extracted = tar.extractfile(member)
                    assert extracted is not None
                    (dst / name).write_bytes(extracted.read())
        # A different host's pointer: this host's own copy is the right one.
        (dst / "project_dir").write_text("/somewhere/else")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        capsys.readouterr()

        ret = restore_main(
            [str(tarball), "--mode", "merge", "--components", "config", "--force"]
            + unpinnable_argv()
        )

        out = capsys.readouterr().out
        assert ret == 0
        assert "project_dir" not in out
        assert "✅ config" in out


class TestNestingBound:
    def test_depth_is_counted_per_container_level(self):
        from kiro_crew.user_json import exceeds_nesting

        assert not exceeds_nesting(1, limit=0)
        assert not exceeds_nesting({"a": [1]}, limit=2)
        assert exceeds_nesting({"a": [1]}, limit=1)
        assert exceeds_nesting({"a": 1, "b": {"c": {}}}, limit=2)

    def test_a_hostile_depth_is_measured_without_recursing(self):
        from kiro_crew.user_json import exceeds_nesting

        doc: object = 1
        for _ in range(50_000):
            doc = [doc]
        assert exceeds_nesting(doc)
