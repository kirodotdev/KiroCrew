"""``kirocrew setup``'s late prompts do not import the gateway or dashboard graph.

Between the timezone answer and the cloud step's ``Launch ... on AWS now?``
question the wizard reads the dashboard port and the deployment's provisioner
list. Reading them through ``cli_server`` and ``dashboard/handlers_cloud.py``
imported both graphs cold, which kept the terminal silent for seconds; an Enter
typed into that silence then answered the AWS question the moment it printed,
so setup looked as if it needed a second Enter. Checked in a fresh interpreter,
since any earlier test in the worker may already have loaded either module.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import kiro_crew
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import default_timeout

_HEAVY = ("kiro_crew.cli_server", "kiro_crew.dashboard.handlers_cloud")


def test_port_and_cloud_lane_reads_leave_the_heavy_graph_unloaded(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(Path(kiro_crew.__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    )
    env["KIROCREW_HOME"] = str(tmp_path / "home")
    env.pop("KIROCREW_PORT", None)
    env.pop("KIROCREW_BOUND_PORT", None)
    probe = (
        "import sys\n"
        "from kiro_crew import cli_setup\n"
        "port = cli_setup._configured_port()\n"
        "offered = cli_setup._builtin_cloud_lane_offered()\n"
        f"print(port, offered, sorted(m for m in {_HEAVY!r} if m in sys.modules))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        check=True,
        env=env,
        cwd=tmp_path,
        timeout=default_timeout(),
        **UTF8_TEXT,
    )
    assert out.stdout.splitlines()[-1] == "5476 True []", out.stderr
