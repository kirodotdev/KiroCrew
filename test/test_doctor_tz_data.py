"""The ``tz data`` row of ``kirocrew doctor`` and the tzdata floor it backs.

Stale timezone data still resolves every zone name, so a job scheduled in a zone
whose rules changed runs at the old rule's hour with no warning anywhere. The
row reads the rules behaviourally: IANA tzdb 2026b keeps America/Vancouver on
UTC-7 all year, and older data still moves it back to UTC-8 each November.
"""

from __future__ import annotations

import configparser
import re
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew import cli_doctor
from kiro_crew.doctor_checks import install

_REPO = Path(__file__).resolve().parents[1]


class _FakeZoneInfo:
    """Stands in for ``zoneinfo.ZoneInfo``: every zone is one fixed UTC offset."""

    offset: timedelta | None = None
    calls: list[str] = []

    @classmethod
    def no_cache(cls, key: str):
        cls.calls.append(key)
        if cls.offset is None:
            raise install.zoneinfo.ZoneInfoNotFoundError(key)
        return timezone(cls.offset)


@pytest.fixture
def zone_data(monkeypatch: pytest.MonkeyPatch):
    """Pin the zone data the probe reads, and the pip channel its remedy names."""
    _FakeZoneInfo.offset = None
    _FakeZoneInfo.calls = []
    monkeypatch.setattr(install.zoneinfo, "ZoneInfo", _FakeZoneInfo)
    monkeypatch.setattr(cli_doctor, "pip_install_channel_available", lambda: True)
    monkeypatch.setattr(cli_doctor, "pip_install_command_for", lambda *s: "PIP " + " ".join(s))
    monkeypatch.setattr(install, "_tz_data_from_system", lambda: False)
    return _FakeZoneInfo


def test_current_rules_are_a_clean_row(zone_data, capsys) -> None:
    zone_data.offset = timedelta(hours=-7)
    issues: list[str] = []
    install._doctor_tz_data(issues)
    out = capsys.readouterr().out
    assert out == "  tz data:     ✅ current (America/Vancouver stays UTC-7 in winter)\n"
    assert issues == []
    assert zone_data.calls == ["America/Vancouver"]


def test_rules_older_than_2026b_are_an_issue(zone_data, capsys) -> None:
    zone_data.offset = timedelta(hours=-8)
    issues: list[str] = []
    install._doctor_tz_data(issues)
    out = capsys.readouterr().out
    assert "  tz data:     ⚠️  out of date (older than IANA tzdb 2026b)" in out
    assert "a job in that zone runs an hour late" in out
    assert "Fix: PIP -U tzdata" in out
    assert "then restart the gateway." in out
    assert issues == ["timezone data out of date"]


def test_stale_system_data_names_the_system_package(zone_data, capsys, monkeypatch) -> None:
    zone_data.offset = timedelta(hours=-8)
    monkeypatch.setattr(install, "_tz_data_from_system", lambda: True)
    issues: list[str] = []
    install._doctor_tz_data(issues)
    out = capsys.readouterr().out
    assert "Fix: update the operating system's timezone data" in out
    assert "PIP" not in out
    assert issues == ["timezone data out of date"]


def test_stale_package_data_without_a_pip_channel_names_an_update(
    zone_data, capsys, monkeypatch
) -> None:
    zone_data.offset = timedelta(hours=-8)
    monkeypatch.setattr(cli_doctor, "pip_install_channel_available", lambda: False)
    issues: list[str] = []
    install._doctor_tz_data(issues)
    out = capsys.readouterr().out
    assert "Fix: update Kiro Crew, then restart the gateway." in out
    assert "PIP" not in out
    assert issues == ["timezone data out of date"]


def test_missing_data_is_an_issue(zone_data, capsys) -> None:
    zone_data.offset = None
    issues: list[str] = []
    install._doctor_tz_data(issues)
    out = capsys.readouterr().out
    assert "  tz data:     ❌ no timezone data resolves America/Vancouver" in out
    assert "Fix: PIP tzdata" in out
    assert issues == ["timezone data missing"]


def test_the_probe_reads_the_zone_at_a_winter_instant_after_the_change() -> None:
    assert install._TZ_PROBE_ZONE == "America/Vancouver"
    assert install._TZ_PROBE_INSTANT.year == 2026
    assert install._TZ_PROBE_INSTANT.month == 12
    assert install._TZ_PROBE_OFFSET == timedelta(hours=-7)


def test_a_zone_file_on_tzpath_is_system_data(tmp_path: Path, monkeypatch) -> None:
    tree = tmp_path / "zoneinfo"
    (tree / "America").mkdir(parents=True)
    monkeypatch.setattr(install.zoneinfo, "TZPATH", (str(tree),))
    assert install._tz_data_from_system() is False
    (tree / "America" / "Vancouver").write_bytes(b"TZif")
    assert install._tz_data_from_system() is True


def test_no_tzpath_is_package_data(monkeypatch) -> None:
    monkeypatch.setattr(install.zoneinfo, "TZPATH", ())
    assert install._tz_data_from_system() is False


def _declared_tzdata_floor() -> tuple[int, int]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_REPO / "setup.cfg", encoding="utf-8")
    lines = parser["options"]["install_requires"].splitlines()
    specs = [line.strip() for line in lines if line.strip().startswith("tzdata")]
    assert len(specs) == 1, specs
    match = re.search(r">=\s*(\d+)\.(\d+)", specs[0])
    assert match, specs[0]
    return int(match.group(1)), int(match.group(2))


def test_the_tzdata_floor_carries_the_2026b_rules() -> None:
    # tzdata 2026.2 is the first release built from IANA tzdb 2026b.
    assert _declared_tzdata_floor() >= (2026, 2)
