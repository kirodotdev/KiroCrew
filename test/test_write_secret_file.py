"""Tests for _write_secret_file helper."""

from __future__ import annotations

import stat
from unittest.mock import patch

import pytest

from kiro_crew.dashboard.server import _write_secret_file


class TestWriteSecretFile:
    def test_happy_path(self, tmp_path):
        secret_path = tmp_path / ".local_secret"

        _write_secret_file(secret_path, "my-secret")

        assert secret_path.read_text() == "my-secret"
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600

    def test_os_open_fails_removes_file(self, tmp_path):
        secret_path = tmp_path / ".local_secret"

        with patch("kiro_crew.dashboard.server.os.open", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                _write_secret_file(secret_path, "s")

        assert not secret_path.exists()

    def test_unlink_failure_still_raises_original(self, tmp_path):
        secret_path = tmp_path / ".local_secret"

        with patch("kiro_crew.dashboard.server.os.open", side_effect=OSError("fail")), \
             patch.object(type(secret_path), "unlink", side_effect=OSError("unlink fail")):
            with pytest.raises(OSError, match="fail"):
                _write_secret_file(secret_path, "s")
