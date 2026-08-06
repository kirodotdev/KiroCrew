"""Tests for gatewayd diagnostic helpers (_count_open_fds, _read_rss_kb, _snapshot_state)."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.mcp_gateway.gatewayd import (
    _count_open_fds,
    _read_rss_kb,
    _snapshot_state,
)


class TestCountOpenFds:
    """Verify _count_open_fds returns a positive int on supported platforms."""

    def test_returns_positive_on_current_platform(self) -> None:
        """We're running on Linux CI — expect a real fd count."""
        result = _count_open_fds()
        # On Linux (and macOS with /dev/fd), this should be > 0.
        if sys.platform in ("linux", "darwin"):
            assert result > 0
        else:
            # On truly unsupported platforms, -1 is acceptable.
            assert result == -1 or result > 0

    def test_returns_minus_one_when_all_sources_fail(self) -> None:
        """If /proc/self/fd, /dev/fd, and Win32 all fail, return -1."""
        with patch("os.listdir", side_effect=OSError("mocked")):
            with patch.object(sys, "platform", "linux"):
                result = _count_open_fds()
        assert result == -1

    def test_proc_self_fd_preferred_on_linux(self) -> None:
        """On Linux, /proc/self/fd is used and returns a sane count."""
        if sys.platform != "linux":
            pytest.skip("Linux-only test")
        result = _count_open_fds()
        # The interpreter itself holds at least stdin/stdout/stderr.
        assert result >= 3


class TestReadRssKb:
    """Verify _read_rss_kb returns a positive int on supported platforms."""

    def test_returns_positive_on_current_platform(self) -> None:
        """We're running on Linux — expect a real RSS value."""
        result = _read_rss_kb()
        if sys.platform in ("linux", "darwin"):
            assert result > 0
        else:
            assert result == -1 or result > 0

    def test_returns_minus_one_when_all_sources_fail(self) -> None:
        """If /proc/self/status and resource both fail, return -1."""
        with patch("builtins.open", side_effect=OSError("mocked")):
            with patch.dict("sys.modules", {"resource": None}):
                with patch.object(sys, "platform", "linux"):
                    result = _read_rss_kb()
        assert result == -1

    def test_rss_is_in_kilobytes(self) -> None:
        """RSS should be in a plausible range for a Python process (> 1MB)."""
        result = _read_rss_kb()
        if result == -1:
            pytest.skip("Not available on this platform")
        # A Python process should use at least ~1MB = 1024 KB.
        assert result > 1024


class TestSnapshotState:
    """Verify _snapshot_state includes all expected fields including pid."""

    def test_snapshot_has_pid_field(self) -> None:
        """The snapshot dict must include a pid field matching os.getpid()."""
        server = MagicMock()
        server.is_serving.return_value = True
        pool = MagicMock()
        pool._backends = {}
        connections: set = set()

        result = _snapshot_state(
            server=server,
            pool=pool,
            connections=connections,
            task_count=5,
        )

        assert "pid" in result
        assert result["pid"] == os.getpid()

    def test_snapshot_has_all_fields(self) -> None:
        """The snapshot dict must contain the expected diagnostic fields."""
        server = MagicMock()
        server.is_serving.return_value = True
        pool = MagicMock()
        pool._backends = {"k": "v"}
        connections: set = set()

        result = _snapshot_state(
            server=server,
            pool=pool,
            connections=connections,
            task_count=10,
        )

        expected_keys = {
            "ts_iso",
            "ts_epoch",
            "pid",
            "is_serving",
            "task_count",
            "fd_count",
            "rss_kb",
            "pool_size",
            "connections_in_flight",
        }
        assert set(result.keys()) == expected_keys
        assert result["task_count"] == 10
        assert result["pool_size"] == 1
        assert result["connections_in_flight"] == 0
        assert result["is_serving"] is True

    def test_snapshot_fd_and_rss_are_populated(self) -> None:
        """On Linux, fd_count and rss_kb should be real values, not -1."""
        if sys.platform != "linux":
            pytest.skip("Linux-only test")
        server = MagicMock()
        server.is_serving.return_value = True
        pool = MagicMock()
        pool._backends = {}
        connections: set = set()

        result = _snapshot_state(
            server=server,
            pool=pool,
            connections=connections,
            task_count=0,
        )

        assert result["fd_count"] > 0
        assert result["rss_kb"] > 0
