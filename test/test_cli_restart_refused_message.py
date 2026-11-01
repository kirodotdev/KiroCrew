"""What ``kirocrew restart`` says when its replacement gateway exits at once.

The replacement is refused when another process still owns the config dir, which
on a desktop install is the app's own supervisor. That owner's gateway keeps
serving the port, so "Nothing is serving port N" may only be printed after a
probe finds nothing there. The exit code and the audit record stay the same in
both cases; only the words change.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
import test_cli_restart_readiness as readiness
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
)

from kiro_crew.gateway_lock import LockHolder, LockProbeError

# A pid no test signals: every process here is a mock or a patched probe.
_HOLDER_PID = 2**22 + 17


def _drive_died(ready_status: int, owner: tuple[int, bool] | None):
    """Run ``_restart`` with a replacement that exits 1 and a probe answering *ready_status*."""
    with patch("kiro_crew.cli_server._config_dir_owner", return_value=owner):
        return readiness.TestRestartReadinessVerdict()._drive(
            poll=1, ready_status=ready_status, marker_pid=[None, None]
        )


def _audit_resources(mock_sel) -> list[str]:
    return [c.kwargs["resources"] for c in mock_sel.log_api_access.call_args_list]


class TestDiedReplacementWording:
    def test_a_port_nobody_answers_keeps_the_nothing_is_serving_wording(self, capsys):
        code, mock_sel, _url = _drive_died(ready_status=0, owner=None)

        assert code == 1
        out = capsys.readouterr().out
        assert "died immediately (exit status 1). Nothing is serving port" in out
        assert "still serving" not in out

    @pytest.mark.parametrize("ready_status", [200, 503, 401, 404])
    def test_a_port_that_still_answers_is_never_called_unserved(self, capsys, ready_status):
        """Any HTTP answer proves only HTTP, including an unrelated server's 404."""
        code, mock_sel, mock_token_url = _drive_died(
            ready_status=ready_status, owner=(_HOLDER_PID, False)
        )

        assert code == 1
        out = capsys.readouterr().out
        assert "Nothing is serving" not in out
        assert "Replacement gateway (pid 4321) exited at once (exit status 1)." in out
        assert re.search(r"\. Port \d+ still answers HTTP\.\n", out)
        assert "A gateway is still serving port" not in out
        assert f"pid {_HOLDER_PID} still owns" in out
        assert "so the replacement was refused." in out
        assert "desktop app" not in out
        assert "kirocrew logs -f" in out
        # Behaviour is unchanged: same exit code, same single denied audit, no token chase.
        assert [c.kwargs["outcome"] for c in mock_sel.log_api_access.call_args_list] == ["denied"]
        assert "reason=replacement_died exit=1" in _audit_resources(mock_sel)[0]
        mock_token_url.assert_not_called()

    def test_a_desktop_owner_is_named_when_it_is_detectable(self, capsys):
        code, mock_sel, _url = _drive_died(ready_status=200, owner=(_HOLDER_PID, True))

        assert code == 1
        out = capsys.readouterr().out
        assert "Nothing is serving" not in out
        assert re.search(r"\. Port \d+ still answers HTTP\.\n", out)
        assert f"The Kiro Crew desktop app (pid {_HOLDER_PID}) owns" in out
        assert "Restart it from the app." in out
        assert "reason=replacement_died exit=1" in _audit_resources(mock_sel)[0]

    @pytest.mark.parametrize("ready_status", [200, 404])
    def test_no_owner_is_claimed_when_the_lock_names_none(self, capsys, ready_status):
        """A port answered by something the lock does not name gets no owner line."""
        code, _sel, _url = _drive_died(ready_status=ready_status, owner=None)

        assert code == 1
        out = capsys.readouterr().out
        assert re.search(r"\. Port \d+ still answers HTTP\.\n", out)
        assert "Nothing is serving" not in out
        assert "A gateway is still serving port" not in out
        assert "owns" not in out
        assert "refused" not in out
        assert "kirocrew logs -f" in out

    def test_the_owner_is_only_looked_up_again_when_the_port_answers(self, capsys):
        """The one lookup is the up-front desktop check; the wording adds none."""
        with patch("kiro_crew.cli_server._config_dir_owner", return_value=None) as mock_desktop:
            code, _sel, _url = readiness.TestRestartReadinessVerdict()._drive(
                poll=1, ready_status=0, marker_pid=[None, None]
            )
        assert mock_desktop.call_count == 1
        assert code == 1
        out = capsys.readouterr().out
        assert "Nothing is serving port" in out
        assert "owns" not in out


class TestConfigDirOwner:
    @staticmethod
    def _holder_pid(*, holder=None, exe=None, cmdline="", raises=None):
        from kiro_crew import cli_server

        lock = (
            patch("kiro_crew.cli_server.lock_holder", side_effect=raises)
            if raises is not None
            else patch("kiro_crew.cli_server.lock_holder", return_value=holder)
        )
        with (
            lock,
            patch("kiro_crew.cli_server.platform_compat.process_executable_path", return_value=exe),
            patch(
                "kiro_crew.cli_server.platform_compat.process_command_line", return_value=cmdline
            ),
        ):
            return cli_server._config_dir_owner()

    _LIVE = LockHolder(pid=_HOLDER_PID, alive=True, source="flock_owner")

    @pytest.mark.parametrize(
        "exe",
        [
            "/Applications/Kiro Crew.app/Contents/Resources/backend-dist/bin/python3.12",
            "/opt/kiro-crew/resources/backend-dist/bin/python3",
        ],
    )
    def test_a_holder_running_from_the_bundle_is_the_desktop(self, exe):
        assert self._holder_pid(holder=self._LIVE, exe=exe) == (_HOLDER_PID, True)

    def test_windows_falls_back_to_the_command_line(self):
        cmdline = r'"C:\Program Files\Kiro Crew\resources\backend-dist\python.exe" -m kiro_crew'
        assert self._holder_pid(holder=self._LIVE, exe=None, cmdline=cmdline) == (
            _HOLDER_PID,
            True,
        )

    @pytest.mark.parametrize(
        "exe",
        [
            "/home/u/.venv/bin/python3.12",
            # A name that merely contains the marker is not the bundle directory.
            "/home/u/my-backend-dist-tools/bin/python3",
        ],
    )
    def test_a_holder_outside_the_bundle_is_not_the_desktop(self, exe):
        assert self._holder_pid(holder=self._LIVE, exe=exe) == (_HOLDER_PID, False)

    def test_an_unreadable_holder_is_named_but_not_the_desktop(self):
        assert self._holder_pid(holder=self._LIVE, exe=None, cmdline="") == (_HOLDER_PID, False)

    def test_a_free_lock_names_nobody(self):
        free = LockHolder(pid=None, alive=False, source="none")
        exe = "/opt/kiro-crew/resources/backend-dist/bin/python3"
        assert self._holder_pid(holder=free, exe=exe) is None

    @pytest.mark.parametrize(
        "exc", [LockProbeError(Path("gateway.lock"), OSError("eio")), OSError("eperm")]
    )
    def test_an_indeterminate_lock_probe_is_not_detectable(self, exc):
        assert self._holder_pid(raises=exc) is None
