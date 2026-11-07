"""The Settings panel's permission probe child (``computer_use/permission_probe.py``).

``GET /api/computer-use/config`` spawns this module under the gateway's own
interpreter, not ``kirocrew computer doctor --json``: the CLI's start-up alone can
outlast the handler's probe timeout, and a timed-out probe makes both permission
rows read "Could not check" on a host that holds both grants. These tests pin the
child's output against the handler's parser, its degrade value, and its import
closure, which must stay off the CLI and the agent-spec writer.

The in-process tests run against the suite's fake backend (``conftest_resets``).
The one real child registers that same fake backend before probing, so no test
reaches a native accessibility or capture call. It imports every real driver module
first, which loads no native library, so their imports count against the closure
the production probe pays.

The module is resolved through the handler's own ``PERMISSION_PROBE_MODULE`` in a
fixture rather than imported at the top: the name the handler spawns is the name
under test, and a missing module fails these tests one by one instead of aborting
collection for every other file in the same run.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from types import ModuleType

import pytest

from kiro_crew.computer_use.types import (
    PERMISSION_GRANTED,
    PERMISSION_UNKNOWN,
    PermissionProbe,
)
from kiro_crew.dashboard.handlers import computer_use as cu_api
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

#: Modules whose start-up cost is what pushed the probe past its timeout: the whole
#: CLI, and the agent-spec writer the doctor's backend projection loads.
_HEAVY_MODULES = ("kiro_crew.cli", "kiro_crew.agent", "kiro_crew.computer_use.cli")

#: The drivers ``backend.select_default_backend`` can choose. The child imports all
#: three, so a heavy import added to any driver fails here on every runner.
_DRIVER_MODULES = (
    "kiro_crew.computer_use.linux_driver",
    "kiro_crew.computer_use.macos_driver",
    "kiro_crew.computer_use.windows_driver",
)

_FAKE_GRANTED = {
    "accessibility": PERMISSION_GRANTED,
    "screen_recording": PERMISSION_GRANTED,
    "responsible_hint": "fake backend",
}


@pytest.fixture(name="permission_probe")
def permission_probe_fixture() -> ModuleType:
    """The module the handler spawns, imported by the name it spawns."""
    return importlib.import_module(cu_api.PERMISSION_PROBE_MODULE)


def test_the_handler_spawns_a_module_with_an_entry_point(permission_probe):
    assert permission_probe.__name__ == cu_api.PERMISSION_PROBE_MODULE
    assert callable(permission_probe.main)


def test_main_prints_the_block_the_handler_parses(capsys, permission_probe):
    assert permission_probe.main() == 0
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1
    assert cu_api._normalize_permissions(json.loads(out)) == _FAKE_GRANTED


def test_a_driver_that_cannot_probe_answers_unknown_not_missing(monkeypatch, permission_probe):
    class _Broken:
        def probe_permissions(self) -> PermissionProbe:
            raise OSError("the accessibility frameworks could not be loaded")

    monkeypatch.setattr("kiro_crew.computer_use.backend.get_shared_backend", lambda: _Broken())
    assert permission_probe.probe_payload() == {
        "accessibility": PERMISSION_UNKNOWN,
        "screen_recording": PERMISSION_UNKNOWN,
        "responsible_hint": "",
    }


def test_a_driver_that_cannot_be_built_answers_unknown(monkeypatch, permission_probe):
    def _no_driver():
        raise RuntimeError("the computer-use driver could not be constructed")

    monkeypatch.setattr("kiro_crew.computer_use.backend.get_shared_backend", _no_driver)
    assert permission_probe.probe_payload()["accessibility"] == PERMISSION_UNKNOWN


def test_running_the_probe_never_loads_the_cli_or_the_spec_writer():
    code = (
        "import json, sys\n"
        "from kiro_crew.computer_use.backend import register_computer_use_backend\n"
        "from kiro_crew.testing.fake_computer_use import FakeComputerUseBackend\n"
        "register_computer_use_backend(FakeComputerUseBackend)\n"
        f"import {', '.join(_DRIVER_MODULES)}\n"
        f"import {cu_api.PERMISSION_PROBE_MODULE} as permission_probe\n"
        "permission_probe.main()\n"
        f"print(json.dumps(sorted(m for m in {_HEAVY_MODULES!r} if m in sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    probe_line, loaded_line = result.stdout.strip().splitlines()[-2:]
    assert cu_api._normalize_permissions(json.loads(probe_line)) == _FAKE_GRANTED
    assert json.loads(loaded_line) == []
