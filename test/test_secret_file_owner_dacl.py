"""Secret-bearing files are restricted to their owner on Windows too (#4543).

``chmod_safe(path, 0o600)`` is a no-op on Windows, so every file it was the
lockdown for stayed under an inherited DACL. NTFS reports ``0o666`` whatever the
DACL says, so no mode assertion can catch a regression; these tests assert the
ROUTING instead: each writer hands its file to ``platform_compat.restrict_to_owner``
(an owner-only DACL on Windows, ``chmod 0o600`` on POSIX). The Windows call is
mocked, so the suite runs on Linux CI.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from kiro_crew import owner_only_files, platform_compat

_SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(platform_compat, "restrict_to_owner", lambda p: calls.append(str(p)))
    return calls


def _names(calls: list[str]) -> list[str]:
    return [Path(c).name for c in calls]


def test_mochi_queue_temp_is_restricted(tmp_path: Path, seen: list[str]) -> None:
    from kiro_crew.apps.builtins.mochi.queue_file import write_queue_atomic

    target = tmp_path / "queue.json"
    write_queue_atomic(target, {"planned_at": "a", "planned_until": "b", "items": []})
    assert any(n.startswith("queue.json.tmp.") for n in _names(seen)), seen
    assert target.exists()


def test_mochi_activity_log_temp_is_restricted(tmp_path: Path, seen: list[str]) -> None:
    from kiro_crew.apps.builtins.mochi.activity_log import _write_store

    target = tmp_path / "activity.json"
    assert _write_store(target, {"date": "2026-01-01", "entries": []}) is True
    assert any(n.startswith("activity.json.tmp.") for n in _names(seen)), seen


def test_crew_companion_store_temp_is_restricted(tmp_path: Path, seen: list[str]) -> None:
    from kiro_crew.apps.builtins.crew_companion.store import CompanionStore

    store = CompanionStore(tmp_path)
    with store._lock:
        store._save_locked()
    assert any(n.startswith("crew-companion-reminders.json.tmp.") for n in _names(seen)), seen


def test_appearance_colour_maps_temp_is_restricted(tmp_path: Path, seen: list[str]) -> None:
    from kiro_crew.appearance_packs.store import AppearanceStore

    store = AppearanceStore(tmp_path)
    store._save_colours()
    assert any(n.startswith("crew-companion-colours.json.tmp.") for n in _names(seen)), seen


def test_telemetry_salt_is_restricted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seen: list[str]
) -> None:
    from kiro_crew.dashboard import handlers_system

    monkeypatch.setattr(handlers_system, "config_dir", lambda: tmp_path)
    salt = handlers_system._get_telemetry_salt()
    assert (tmp_path / "telemetry_salt").read_bytes() == salt
    assert len(seen) == 1 and Path(seen[0]).parent == tmp_path, seen


def test_redaction_allow_list_is_restricted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seen: list[str]
) -> None:
    from kiro_crew.security import redaction_allow

    target = tmp_path / "redaction-allow" / "hosts.json"
    monkeypatch.setattr(redaction_allow, "_path_override", target)
    monkeypatch.setattr(redaction_allow, "_snapshot", None)
    redaction_allow._write({"ws": ["example.com"]})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ws": ["example.com"]}
    assert _names(seen) == ["hosts.tmp"], seen


def test_backend_log_is_restricted_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seen: list[str]
) -> None:
    from kiro_crew.apps import backend

    log_path = tmp_path / "backend.log"
    log_path.write_text("previous session\n", encoding="utf-8")  # an existing install's log
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    with backend._open_backend_log(log_path) as fh:
        fh.write("x")
    assert seen == [str(log_path)]


def test_backend_log_open_leaves_posix_to_the_creation_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seen: list[str]
) -> None:
    from kiro_crew.apps import backend

    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    with backend._open_backend_log(tmp_path / "backend.log") as fh:
        fh.write("x")
    assert seen == []  # POSIX: owner_only_opener + the startup sweep already cover it


def test_a_failed_restrict_warns_and_the_write_still_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from kiro_crew.apps.builtins.mochi.queue_file import write_queue_atomic

    def _deny(p: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _deny)
    target = tmp_path / "queue.json"
    write_queue_atomic(target, {"planned_at": "a", "planned_until": "b", "items": []})
    assert target.exists()
    assert "Cannot restrict a secret-bearing file" in caplog.text
    assert str(tmp_path) not in caplog.text  # the path is never logged


def test_helper_returns_false_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _deny(p: object) -> None:
        raise OSError("nope")

    monkeypatch.setattr(platform_compat, "restrict_to_owner", _deny)
    assert owner_only_files.restrict_file_to_owner_soft(tmp_path / "f") is False


#: chmod_safe(..., 0o600) sites that are AF_UNIX sockets, which do not exist on
#: Windows (a socket's DACL is not what guards it there), so the no-op is correct.
_SOCKET_SITES = frozenset({"mcp_gateway/socketsec.py", "mcp_gateway/transport.py"})


def test_no_file_lockdown_is_left_on_chmod_safe() -> None:
    """Guard: a new ``chmod_safe(x, 0o600)`` would silently skip Windows again."""
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "chmod_safe" not in text:
            continue
        rel = path.relative_to(_SRC).as_posix()
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call) or len(node.args) != 2:
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            mode = node.args[1]
            if (
                name == "chmod_safe"
                and isinstance(mode, ast.Constant)
                and mode.value == 0o600
                and rel not in _SOCKET_SITES
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        "use owner_only_files.restrict_file_to_owner_soft for a secret at rest: " f"{offenders}"
    )


def test_guard_sees_the_socket_sites() -> None:
    """The allow-list is live: each named file still has its socket chmod."""
    for rel in _SOCKET_SITES:
        text = (_SRC / rel).read_text(encoding="utf-8")
        assert re.search(r"chmod_safe\(.*0o600\)", text), rel
