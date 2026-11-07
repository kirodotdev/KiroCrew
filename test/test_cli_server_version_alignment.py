"""``kirocrew status`` says whether the gateway runs the package selected to run.

The selected package is the live-target checkout when one is pinned, else the
installed package read from disk; both sides are the string ``kirocrew --version``
prints, so two builds of one release differ.
"""

from __future__ import annotations

import io
import json
import os
import time
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew
from kiro_crew import cli_server
from kiro_crew.service import live_target


@pytest.fixture(autouse=True)
def _isolate_pointer(tmp_path, _floor_monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _floor_monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: home)


def _package(root: Path, init_text: str, stamp: str | None = None) -> Path:
    package = root / "src" / "kiro_crew"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(init_text, encoding="utf-8")
    if stamp is not None:
        (package / "BUILD_VERSION").write_text(stamp, encoding="utf-8")
    return package


def _pin_checkout(root: Path, init_text: str, stamp: str | None = None) -> None:
    _package(root, init_text, stamp)
    kcbin = live_target.target_bin(root)
    kcbin.parent.mkdir(parents=True)
    kcbin.write_text("#!/bin/sh\n")
    kcbin.chmod(0o755)
    live_target.write_target(root)


def test_selected_version_reads_the_pinned_checkout_with_its_stamp(tmp_path) -> None:
    _pin_checkout(tmp_path / "checkout", '__version__ = "0.9.0"\n', stamp="0.9.0.2\n")

    assert live_target.selected_version()[0] == "0.9.0.2"


def test_selected_version_unpinned_reads_the_installed_package_from_disk(
    tmp_path, monkeypatch
) -> None:
    # The imported __version__ is not the answer: an in-place upgrade rewrites the
    # files under a process that keeps the version it imported.
    package = _package(tmp_path / "install", '__version__ = "99.0.0"\n')
    monkeypatch.setattr(kiro_crew, "__file__", str(package / "__init__.py"))

    assert live_target.selected_version()[0] == "99.0.0"


def test_selected_version_without_a_version_literal_is_unknown(tmp_path) -> None:
    _pin_checkout(tmp_path / "checkout", "__version__ = compute()\n")

    assert live_target.selected_version() == (None, None)


def test_selected_version_counts_an_unpin_as_a_change() -> None:
    # Unpinned, the installed package's files can be older than the unpin itself;
    # a pointer stamped an hour ahead proves its change time is read.
    assert live_target.restore(None)
    unpinned_at = int(time.time()) + 3600
    os.utime(live_target.pointer_path(), (unpinned_at, unpinned_at))

    assert live_target.selected_version()[1] == unpinned_at


def test_status_prints_aligned_when_the_gateway_runs_the_selected_build(monkeypatch, capsys):
    monkeypatch.setattr(live_target, "selected_version", lambda: ("0.13.0", time.time()))
    payload = {"uptime": "1h", "version": "0.13.0", "start_time": time.time() - 3600}

    @contextmanager
    def _urlopen(url, timeout):
        yield io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(cli_server, "loopback_urlopen", _urlopen)
    monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 7777)
    cli_server._status(SimpleNamespace(port=None))

    assert "  Version:     aligned (0.13.0)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("running", "expected"),
    [
        ("0.12.0", "version_skew: the gateway runs 0.12.0, 0.13.0 is selected"),
        ("0.13.0", "aligned (0.13.0)"),
    ],
    ids=["skew", "aligned"],
)
def test_token_auth_status_prints_version_without_age(monkeypatch, capsys, running, expected):
    from kiro_crew.doctor_checks import services

    monkeypatch.setattr(live_target, "selected_version", lambda: ("0.13.0", 1.0))
    monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 7777)

    def _urlopen(url, timeout):
        assert url == "http://127.0.0.1:7777/api/status"
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    def _running_version(port):
        assert port == 7777
        return running

    monkeypatch.setattr(cli_server, "loopback_urlopen", _urlopen)
    monkeypatch.setattr(services, "_running_gateway_version", _running_version)
    cli_server._status(SimpleNamespace(port=None))

    out = capsys.readouterr().out
    assert "Kiro Crew gateway is running (token auth enabled)." in out
    assert "For detailed stats, see the Overview page in the dashboard." in out
    assert f"  Version:     {expected}" in out
    assert "for at least" not in out


def test_two_builds_of_one_release_are_skew_aged_from_the_selection() -> None:
    now = time.time()
    payload = {"version": "0.13.0.1", "start_time": now - 86400}

    line = cli_server._format_version_line(payload, "0.13.0.2", now - 7200)

    assert line.startswith("version_skew for at least 2h 0m:")
    assert "the gateway runs 0.13.0.1, 0.13.0.2 is selected" in line


def test_skew_is_aged_from_the_gateway_start_when_it_started_later() -> None:
    now = time.time()
    payload = {"version": "0.12.0", "start_time": now - 60}

    line = cli_server._format_version_line(payload, "0.13.0", now - 7200)

    assert line.startswith("version_skew for at least 1m ")


@pytest.mark.parametrize(
    ("running", "selected", "names"),
    [(None, "0.13.0", "the gateway did not report"), ("0.13.0", None, "the selected package")],
    ids=["running", "selected"],
)
def test_an_unreadable_version_is_unknown_and_names_the_side(running, selected, names) -> None:
    line = cli_server._format_version_line({"version": running}, selected, None)

    assert line.startswith("unknown (")
    assert names in line


def test_doctor_reads_the_same_selected_version_as_status(tmp_path, monkeypatch, capsys) -> None:
    # A pinned checkout at another version than the imported __version__: doctor
    # compares the gateway with the pin, as status does, and finds it aligned.
    from kiro_crew.doctor_checks import services

    _pin_checkout(tmp_path / "checkout", '__version__ = "0.9.0"\n')
    monkeypatch.setattr(services, "_running_gateway_version", lambda port: "0.9.0")
    assert kiro_crew.__version__ != "0.9.0"
    issues: list[str] = []

    services._doctor_running_version(7777, issues)

    assert "running:     ✅ 0.9.0 (same as on disk)" in capsys.readouterr().out
    assert issues == []
    assert cli_server._format_version_line(
        {"version": "0.9.0"}, *live_target.selected_version()
    ) == ("aligned (0.9.0)")


def test_doctor_names_an_unreadable_selected_version_without_an_issue(monkeypatch, capsys) -> None:
    from kiro_crew.doctor_checks import services

    monkeypatch.setattr(live_target, "selected_version", lambda: (None, None))
    monkeypatch.setattr(services, "_running_gateway_version", lambda port: "0.9.0")
    issues: list[str] = []

    services._doctor_running_version(7777, issues)

    assert "0.9.0 (on disk unknown)" in capsys.readouterr().out
    assert issues == []
