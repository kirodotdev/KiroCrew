"""What every backend test pays at start-up: the ``kiro_crew`` modules loaded
before the first test runs.

``test/conftest.py`` and its ``conftest_*`` parts are imported by every pytest
process, and a product import at their module level is paid by every collection,
whether or not a test uses the fixture it serves. One such line,
``from kiro_crew.slack.handler import ...`` in ``conftest_slack.py``, reached
almost the whole package: collecting a file with no product imports loaded
1,433 ``kiro_crew`` modules. Those imports now sit in the fixtures and hooks that
use them.

The test collects THIS file in a child pytest and counts the ``kiro_crew``
modules in ``sys.modules`` when collection finishes. This file imports nothing
from ``kiro_crew`` at module level, so the count is what the conftests cost. It
is a ratchet: when a change lowers the count, lower ``_BUDGET`` with it; a new
module-level product import in a conftest belongs in the fixture that needs it.

It counts collection, not the first test: autouse fixtures still import what
they reset when the first test sets up.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

#: Measured 33 on Linux; the slack (7) and ``platform_compat`` (21) imports stay
#: module level because a class and the collect-ignore hook need them at import.
_BUDGET = 40

_TEST_DIR = pathlib.Path(__file__).resolve().parent

_COUNTING_PLUGIN = """\
import importlib.metadata
import os
import sys

# Loaded with -p, so before the conftests: an installed ``kirocrew.plugins``
# companion is hidden from the floor's companion hook, which would otherwise
# import the platform stack to hide it and count that against the conftests.
_real_entry_points = importlib.metadata.entry_points


def _entry_points(**params):
    if params.get("group") == "kirocrew.plugins":
        return importlib.metadata.EntryPoints(())
    return _real_entry_points(**params)


importlib.metadata.entry_points = _entry_points


def pytest_collection_finish(session):
    count = sum(1 for m in list(sys.modules) if m == "kiro_crew" or m.startswith("kiro_crew."))
    names = sorted(m for m in list(sys.modules) if m.startswith("kiro_crew"))
    with open(os.environ["KIROCREW_IMPORT_BUDGET_OUT"], "w", encoding="utf-8") as fh:
        fh.write(str(count) + "\\n" + "\\n".join(names))
"""


def test_collection_loads_few_kiro_crew_modules(tmp_path):
    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "kc_import_budget_probe.py").write_text(_COUNTING_PLUGIN, encoding="utf-8")
    out = tmp_path / "count.txt"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(plugin_dir), env.get("PYTHONPATH", "")) if p
    )
    env["KIROCREW_IMPORT_BUDGET_OUT"] = str(out)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "0",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "kc_import_budget_probe",
            "--collect-only",
            str(pathlib.Path(__file__).resolve()),
        ],
        cwd=_TEST_DIR.parent,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    count_line, _, names = out.read_text(encoding="utf-8").partition("\n")
    count = int(count_line)
    assert count <= _BUDGET, (
        f"collecting one test loaded {count} kiro_crew modules (budget {_BUDGET}). "
        "Move the new module-level product import in test/conftest*.py into the "
        f"fixture or hook that uses it. Loaded:\n{names}"
    )


def test_companion_scan_group_matches_discovery():
    """``conftest_floor_configure`` skips its companion import when no entry point of
    this group is installed; the group must be the one discovery reads."""
    from kiro_crew.platform import discovery

    assert discovery.PLUGIN_GROUP == "kirocrew.plugins"
