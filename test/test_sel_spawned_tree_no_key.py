"""A process in a spawned app backend's tree never mints, rotates or signs with a SEL key.

The sandbox hides the key from a backend that writes through the gateway's route, so a
process it starts that builds ``SecurityEventLog()`` finds the key absent, empty or
unreadable. It must not raise, must not create a key (a second chain), and must not write
a row: the row is dropped with one warning, and a key that is readable does not sign.
This holds only where the spawn marker, the app name, the gateway origin and the
per-app secret are all set: those name a backend with the route. A gateway, a process
with the marker alone, and a backend spawned without an origin (no route, key not
masked) still write signed rows.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest
from sel_helpers import _make_event, reset_singleton  # noqa: F401  (autouse fixture)

from kiro_crew.sel import SecurityEventLog

_KEY = Path("trust") / "sel_hmac.key"


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The variables a spawned app backend passes its tree."""
    monkeypatch.setenv("KIROCREW_SPAWNED", "1")
    monkeypatch.setenv("KIROCREW_APP_NAME", "plain-app")
    monkeypatch.setenv("KIROCREW_GATEWAY_ORIGIN", "http://127.0.0.1:8123")
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", "per-app-secret")


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def _last_row(root: Path) -> dict:
    lines = (root / "security_events.jsonl").read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def _drop_two_rows(root: Path, caplog: pytest.LogCaptureFixture) -> None:
    before = _tree(root)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
        log = SecurityEventLog(base_dir=root, sync=True)
        log.log(_make_event(event_id="drop-1"))
        log.log(_make_event(event_id="drop-2"))
    assert _tree(root) == before, "the spawned tree wrote into the SEL directory"
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warned) == 1, [r.getMessage() for r in warned]


def test_a_missing_key_is_not_created(spawned, tmp_path, caplog) -> None:
    _drop_two_rows(tmp_path, caplog)

    assert not (tmp_path / _KEY).exists()


def test_an_empty_key_is_left_alone_and_does_not_raise(spawned, tmp_path, caplog) -> None:
    (tmp_path / "trust").mkdir()
    (tmp_path / _KEY).write_bytes(b"")

    _drop_two_rows(tmp_path, caplog)

    assert (tmp_path / _KEY).read_bytes() == b""


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="Windows has no mode-0 file, and root reads one",
)
def test_an_unreadable_key_is_left_alone_and_does_not_raise(spawned, tmp_path, caplog) -> None:
    (tmp_path / "trust").mkdir()
    key = tmp_path / _KEY
    key.write_bytes(b"k" * 32)
    key.chmod(0)
    try:
        _drop_two_rows(tmp_path, caplog)
    finally:
        key.chmod(0o600)

    assert key.read_bytes() == b"k" * 32


def test_a_readable_valid_key_is_not_used_to_sign(spawned, tmp_path, caplog) -> None:
    (tmp_path / "trust").mkdir()
    (tmp_path / _KEY).write_bytes(b"k" * 32)

    _drop_two_rows(tmp_path, caplog)

    assert (tmp_path / _KEY).read_bytes() == b"k" * 32
    assert not (tmp_path / "security_events.jsonl").exists()


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "queued"])
def test_a_critical_write_raises_so_the_caller_refuses_the_action(spawned, tmp_path, sync) -> None:
    log = SecurityEventLog(base_dir=tmp_path, sync=sync)

    with pytest.raises(OSError):
        log.log(_make_event(event_id="critical-1"), critical=True)

    assert not (tmp_path / "security_events.jsonl").exists()
    assert not (tmp_path / _KEY).exists()


def test_a_non_critical_row_is_dropped_after_a_critical_one_raised(
    spawned, tmp_path, caplog
) -> None:
    with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        with pytest.raises(OSError):
            log.log(_make_event(event_id="critical-2"), critical=True)
        log.log(_make_event(event_id="plain-1"))

    assert not (tmp_path / "security_events.jsonl").exists()
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_a_deny_exception_row_raises_even_when_not_marked_critical(spawned, tmp_path) -> None:
    log = SecurityEventLog(base_dir=tmp_path, sync=True)

    with pytest.raises(OSError):
        log.log(_make_event(event_id="grant-1", event_type="deny_exception"))

    assert not (tmp_path / "security_events.jsonl").exists()


def test_the_deny_gate_refuses_a_grant_whose_row_cannot_be_written(
    spawned, monkeypatch, tmp_path
) -> None:
    from kiro_crew.security.deny_gate import _emit_deny_exception_event

    # Another process owns the route client, so sel() is this process's own writer.
    monkeypatch.setenv("KIROCREW_SEL_CLIENT_PID", str(os.getpid() + 1))
    SecurityEventLog(base_dir=tmp_path, sync=True)

    assert _emit_deny_exception_event("bash", "rm *") is False


def test_a_gateway_without_the_spawn_marker_still_creates_its_key_and_writes(
    monkeypatch, tmp_path
) -> None:
    for name in (
        "KIROCREW_SPAWNED",
        "KIROCREW_APP_NAME",
        "KIROCREW_GATEWAY_ORIGIN",
        "KIROCREW_PROXY_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    log.log(_make_event(event_id="gateway-1"))

    assert len((tmp_path / _KEY).read_bytes()) == 32
    assert "gateway-1" in (tmp_path / "security_events.jsonl").read_text(encoding="utf-8")


def test_the_spawn_marker_without_the_app_variables_still_writes_signed_rows(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("KIROCREW_SPAWNED", "1")
    for name in ("KIROCREW_APP_NAME", "KIROCREW_GATEWAY_ORIGIN", "KIROCREW_PROXY_SECRET"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "trust").mkdir()
    (tmp_path / _KEY).write_bytes(b"k" * 32)

    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    log.log(_make_event(event_id="marker-only-1"))

    assert _last_row(tmp_path)["event_id"] == "marker-only-1"
    assert _last_row(tmp_path)["entry_hash"]


def test_a_backend_spawned_without_an_origin_has_no_route_and_still_signs(
    spawned, monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("KIROCREW_GATEWAY_ORIGIN")
    (tmp_path / "trust").mkdir()
    (tmp_path / _KEY).write_bytes(b"k" * 32)

    log = SecurityEventLog(base_dir=tmp_path, sync=True)
    log.log(_make_event(event_id="no-route-1"))

    assert _last_row(tmp_path)["event_id"] == "no-route-1"
    assert _last_row(tmp_path)["entry_hash"]
    assert (tmp_path / _KEY).read_bytes() == b"k" * 32
