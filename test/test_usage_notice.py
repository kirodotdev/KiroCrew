"""The one-line usage-data notice on CLI and headless starts (``metrics.notice``).

Drives the real check against a real data home: the loaded config, the consent
resolution in ``metrics.provider`` and the stamp file.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from kiro_crew.beacon import PRIVACY_NOTICE_VERSION
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.metrics import notice


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    # The suite pins KIROCREW_TELEMETRY=0 for every test; this module is about
    # the default, so it starts from no env pin at all.
    for name in ("KIROCREW_TELEMETRY", "DO_NOT_TRACK", "KIROCREW_TELEMETRY_DISABLED"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _write(home: Path, doc: dict) -> None:
    (home / "config.json").write_text(json.dumps(doc), encoding="utf-8")


def _run() -> tuple[bool, str]:
    out = io.StringIO()
    printed = notice.maybe_print_notice(KiroCrewConfig.load(), stream=out)
    return printed, out.getvalue()


def test_prints_once_on_a_default_install(home: Path) -> None:
    printed, text = _run()
    assert printed is True
    assert text.strip() == notice.NOTICE_TEXT
    assert "kirocrew telemetry disable" in text and "DO_NOT_TRACK=1" in text
    assert (home / notice.NOTICE_STAMP_FILE).is_file()
    # The second start stays quiet.
    assert _run() == (False, "")


def test_the_notice_is_not_a_recorded_choice(home: Path) -> None:
    """Printing must not acknowledge the notice: export stays off until enable."""
    _run()
    assert KiroCrewConfig.load().dashboard.privacy_ack_version == 0


def test_silent_once_the_current_notice_was_acknowledged(home: Path) -> None:
    _write(home, {"dashboard": {"privacy_ack_version": PRIVACY_NOTICE_VERSION}})
    assert _run() == (False, "")
    assert not (home / notice.NOTICE_STAMP_FILE).exists()


@pytest.mark.parametrize(
    "doc",
    [
        {"telemetry": {"enabled": False}},
        # The heartbeat-off rule: recording loads as off, so nothing to tell.
        {"telemetry": {"beacon_enabled": False}},
    ],
)
def test_silent_when_recording_is_off_in_config(home: Path, doc: dict) -> None:
    _write(home, doc)
    assert _run() == (False, "")


@pytest.mark.parametrize(("name", "value"), [("KIROCREW_TELEMETRY", "0"), ("DO_NOT_TRACK", "1")])
def test_silent_when_an_env_var_turns_recording_off(
    home: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    assert _run() == (False, "")


def test_prints_again_when_the_stamp_cannot_be_written(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refuse(*args: object, **kwargs: object) -> None:
        raise OSError("read-only data home")

    monkeypatch.setattr("kiro_crew.atomic_write.atomic_write", _refuse)
    assert _run()[0] is True
    assert _run()[0] is True


def test_an_unreadable_config_prints_nothing_and_never_raises(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(cls: object) -> None:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    out = io.StringIO()
    assert notice.maybe_print_notice(stream=out) is False
    assert out.getvalue() == ""


def test_the_boot_path_does_not_wait_for_the_notice(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway starts the notice on a daemon thread and goes on booting."""
    started = []
    release = __import__("threading").Event()

    def _slow(cfg: object = None, **kwargs: object) -> bool:
        started.append(cfg)
        release.wait(5)
        return True

    monkeypatch.setattr(notice, "maybe_print_notice", _slow)
    thread = notice.print_notice_in_background(None)
    # Returned while the notice is still blocked.
    assert thread.daemon is True
    assert thread.is_alive()
    release.set()
    thread.join(5)
    assert started == [None]
