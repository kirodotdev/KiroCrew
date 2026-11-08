"""``platform_compat.power_source``: mains, battery or unknown, never raising.

Every reader runs against a FIXTURE: a sysfs tree under ``tmp_path`` on Linux, and
the native query monkeypatched on Windows. macOS has no reader yet and answers
unknown. Reading the real host's power supplies would answer differently on a
laptop, a desktop and CI.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import platform_compat
from kiro_crew.platform_compat import (
    POWER_AC,
    POWER_BATTERY,
    POWER_UNKNOWN,
    _linux_power_source,
    _windows_power_source,
)


def _supply(root, name: str, kind: str, online: str | None = None, scope: str | None = None):
    # Real sysfs names carry a ':' (ucsi-source-psy-USBC000:001), which is not a
    # legal Windows path character; the reader keys on type/scope, never the name.
    path = os.path.join(root, name)
    os.makedirs(path)
    for attr, value in (("type", kind), ("online", online), ("scope", scope)):
        if value is not None:
            with open(os.path.join(path, attr), "w", encoding="utf-8") as fh:
                fh.write(f"{value}\n")


class TestLinuxPowerSource:
    def test_missing_root_is_unknown(self, tmp_path):
        assert _linux_power_source(str(tmp_path / "absent")) == POWER_UNKNOWN

    def test_empty_tree_is_unknown(self, tmp_path):
        assert _linux_power_source(str(tmp_path)) == POWER_UNKNOWN

    def test_no_mains_supply_is_unknown(self, tmp_path):
        # A desktop whose only supplies are batteries -- its own UPS-less board
        # reports none, a wireless mouse reports one -- must not read as a laptop.
        _supply(tmp_path, "hid-mouse-battery", "Battery", online="1", scope="Device")
        _supply(tmp_path, "BAT0", "Battery")
        assert _linux_power_source(str(tmp_path)) == POWER_UNKNOWN

    def test_mains_online_is_ac(self, tmp_path):
        _supply(tmp_path, "AC0", "Mains", online="1")
        _supply(tmp_path, "BAT0", "Battery")
        assert _linux_power_source(str(tmp_path)) == POWER_AC

    def test_mains_offline_is_battery(self, tmp_path):
        _supply(tmp_path, "AC0", "Mains", online="0")
        _supply(tmp_path, "BAT0", "Battery")
        assert _linux_power_source(str(tmp_path)) == POWER_BATTERY

    def test_usb_c_pd_online_with_ac0_offline_is_ac(self, tmp_path):
        # Shaped like a UCSI laptop: the PD charger shows on the USB-C port's
        # supply, which is type USB, scope System.
        _supply(tmp_path, "AC0", "Mains", online="0")
        _supply(tmp_path, "BAT0", "Battery")
        _supply(tmp_path, "ucsi-source-psy-USBC000-001", "USB", online="0", scope="System")
        _supply(tmp_path, "ucsi-source-psy-USBC000-002", "USB", online="1", scope="System")
        assert _linux_power_source(str(tmp_path)) == POWER_AC

    @pytest.mark.parametrize("online", ["2", "3"])
    def test_usb_c_online_programmable_is_ac(self, tmp_path, online):
        # sysfs 'online' 2 = online programmable: a tcpm USB-C PPS charger.
        # Any value other than an explicit 0 is a supply delivering power.
        _supply(tmp_path, "gpio-mains", "Mains", online="0")
        _supply(tmp_path, "BAT0", "Battery")
        _supply(tmp_path, "tcpm-source-psy-0-0022", "USB", online=online)
        assert _linux_power_source(str(tmp_path)) == POWER_AC

    def test_an_empty_online_attribute_is_unreadable(self, tmp_path):
        _supply(tmp_path, "AC0", "Mains", online="0")
        _supply(tmp_path, "BAT0", "Battery")
        _supply(tmp_path, "AC1", "Mains", online="")
        assert _linux_power_source(str(tmp_path)) == POWER_UNKNOWN

    def test_every_external_supply_offline_is_battery(self, tmp_path):
        _supply(tmp_path, "AC0", "Mains", online="0")
        _supply(tmp_path, "BAT0", "Battery")
        _supply(tmp_path, "ucsi-source-psy-USBC000-001", "USB", online="0", scope="System")
        assert _linux_power_source(str(tmp_path)) == POWER_BATTERY

    def test_a_peripheral_usb_supply_is_not_external_power(self, tmp_path):
        _supply(tmp_path, "AC0", "Mains", online="0")
        _supply(tmp_path, "BAT0", "Battery")
        _supply(tmp_path, "usb-peripheral", "USB", online="1", scope="Device")
        assert _linux_power_source(str(tmp_path)) == POWER_BATTERY

    def test_an_idle_usb_c_port_with_no_battery_is_unknown(self, tmp_path):
        # A desktop with a UCSI controller and no Mains supply: every external
        # supply reads offline, but there is no battery to run on.
        _supply(tmp_path, "ucsi-source-psy-USBC000-001", "USB", online="0", scope="System")
        assert _linux_power_source(str(tmp_path)) == POWER_UNKNOWN

    def test_every_external_supply_offline_with_only_a_peripheral_battery_is_unknown(
        self, tmp_path
    ):
        _supply(tmp_path, "AC0", "Mains", online="0")
        _supply(tmp_path, "hid-mouse-battery", "Battery", online="1", scope="Device")
        assert _linux_power_source(str(tmp_path)) == POWER_UNKNOWN

    def test_an_unreadable_supply_is_skipped(self, tmp_path):
        _supply(tmp_path, "AC0", "Mains")  # no 'online' attribute
        _supply(tmp_path, "AC1", "Mains", online="1")
        assert _linux_power_source(str(tmp_path)) == POWER_AC

    def test_an_unreadable_supply_beside_an_offline_one_is_unknown(self, tmp_path):
        # The unreadable USB-C port may be the charger: never call that battery.
        _supply(tmp_path, "AC0", "Mains", online="0")
        _supply(tmp_path, "ucsi-source-psy-USBC000-001", "USB", scope="System")
        assert _linux_power_source(str(tmp_path)) == POWER_UNKNOWN

    def test_only_unreadable_supplies_is_unknown(self, tmp_path):
        _supply(tmp_path, "AC0", "Mains")
        assert _linux_power_source(str(tmp_path)) == POWER_UNKNOWN


class TestWindowsPowerSource:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [(1, POWER_AC), (0, POWER_BATTERY), (255, POWER_UNKNOWN), (None, POWER_UNKNOWN)],
    )
    def test_ac_line_status(self, monkeypatch, line, expected):
        monkeypatch.setattr(platform_compat, "_windows_ac_line_status", lambda: line)
        assert _windows_power_source() == expected


class TestDispatch:
    @pytest.mark.parametrize(
        ("flag", "reader"),
        [
            ("IS_LINUX", "_linux_power_source"),
            ("IS_WINDOWS", "_windows_power_source"),
        ],
    )
    def test_each_platform_reads_its_own_source(self, monkeypatch, flag, reader):
        for name in ("IS_LINUX", "IS_MACOS", "IS_WINDOWS"):
            monkeypatch.setattr(platform_compat, name, name == flag)
        monkeypatch.setattr(platform_compat, reader, lambda: POWER_BATTERY)
        assert platform_compat.power_source() == POWER_BATTERY

    @pytest.mark.parametrize("flag", ["IS_MACOS", None])
    def test_macos_and_any_other_platform_are_unknown(self, monkeypatch, flag):
        for name in ("IS_LINUX", "IS_MACOS", "IS_WINDOWS"):
            monkeypatch.setattr(platform_compat, name, name == flag)
        assert platform_compat.power_source() == POWER_UNKNOWN

    def test_the_windows_query_fails_soft_off_windows(self):
        # Off Windows there is no kernel32 to load; the query must answer None,
        # never raise into a sweep.
        if not platform_compat.IS_WINDOWS:
            assert platform_compat._windows_ac_line_status() is None
