"""The managed temp dir says when it leaves a server too little socket room.

A server binds its Unix socket a few levels below ``TMPDIR``, so a long data
home can push that path past ``sun_path``. The folder stays where it is; the
gateway warns once and a failed Connect check names the workaround.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew import mcp_discovery
from kiro_crew.mcp_discovery import McpServerInfo, _probe_cache, probe_server
from kiro_crew.mcp_gateway import backend_tmp as bt

# A 71-character data home, the length the issue measured on Windows.
_LONG_HOME = "/" + "h" * 70
# The default data home's length on the reporting host.
_SHORT_HOME = "/" + "h" * 26

#: The smallest ``sun_path`` (macOS), which the suite floor pins out of reach.
_REAL_SUN_PATH_BYTES = 104

pytestmark = pytest.mark.usefixtures("_real_socket_budget")


@pytest.fixture
def _real_socket_budget(monkeypatch):
    monkeypatch.setattr(bt, "_SUN_PATH_BYTES", _REAL_SUN_PATH_BYTES, raising=False)


@pytest.mark.parametrize(
    ("home", "suffix", "warns"),
    [
        (_LONG_HOME, "/run/t/0123abcd", True),  # gateway server, 86 bytes
        (_LONG_HOME, "/run/t/0123abcd/t", True),  # Connect check, 88 bytes
        (_SHORT_HOME, "/run/t/0123abcd", False),  # 42 + 54 = 96
        (_SHORT_HOME, "/run/t/0123abcd/t", False),  # 44 + 54 = 98
        ("/" + "h" * 34, "/run/t/0123abcd", False),  # 50 + 54 = 104, the limit
        ("/" + "h" * 35, "/run/t/0123abcd", True),  # 51 + 54 = 105
    ],
)
def test_hint_appears_only_past_the_socket_budget(home: str, suffix: str, warns: bool) -> None:
    path = Path(home + suffix)
    hint = bt.socket_room_hint(path)

    assert bool(hint) is warns
    if warns:
        assert f"{len(str(path))} bytes" in hint
        assert "TEMP, TMP and TMPDIR" in hint


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX filesystem bytes")
def test_a_non_utf8_data_home_is_measured_not_raised() -> None:
    # A POSIX name byte that is not UTF-8 decodes to a surrogate escape.
    path = Path(os.fsdecode(b"/" + b"\xff" * 70 + b"/run/t/0123abcd"))

    assert "86 bytes" in bt.socket_room_hint(path)


def test_a_long_data_home_warns_once_with_the_length(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=bt.__name__):
        bt.warn_low_socket_room(Path(_LONG_HOME + "/run/t/0123abcd"))
        bt.warn_low_socket_room(Path(_LONG_HOME + "/run/t/4567ef89"))

    warnings = [r.getMessage() for r in caplog.records if "socket path" in r.getMessage()]
    assert len(warnings) == 1
    assert "86 bytes" in warnings[0]


def test_a_short_data_home_does_not_warn(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=bt.__name__):
        bt.warn_low_socket_room(Path(_SHORT_HOME + "/run/t/0123abcd"))

    assert not [r for r in caplog.records if "socket path" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_failed_connect_check_carries_the_hint(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / ("h" * 60)
    home.mkdir()
    monkeypatch.setattr(bt, "config_dir", lambda: home)

    def _wrap(argv, *a, env=None, **k):
        return list(argv), dict(env if env is not None else os.environ), None

    monkeypatch.setattr(mcp_discovery, "sandboxed_spawn_argv", _wrap)
    monkeypatch.setattr(
        mcp_discovery,
        "create_subprocess_limited",
        AsyncMock(side_effect=OSError("Failed to initialize MCP Server")),
    )
    server = McpServerInfo(name="room", command=sys.executable, args=["-c", "pass"], env={})
    _probe_cache.clear()
    try:
        await probe_server(server)
    finally:
        _probe_cache.clear()

    assert server.status == "error"
    assert "Failed to initialize MCP Server" in server.error
    assert "TEMP, TMP and TMPDIR" in server.error
