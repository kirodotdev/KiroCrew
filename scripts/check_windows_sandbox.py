"""Run the opt-in Windows sandbox's real, synthetic confinement probe.

This never selects an agent, changes Crew config, starts a model or installs code.
Run from an ordinary Windows terminal; an existing outer sandbox may forbid MXC.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiro_crew.sandbox_windows import (  # noqa: E402
    WINDOWS_SANDBOX_EXE_ENV,
    probe_windows_sandbox,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runner", required=True, type=Path, help="Native kirocrew-windows-sandbox.exe"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if sys.platform != "win32" or not args.runner.is_file():
        parser.error("An existing native kirocrew-windows-sandbox.exe on Windows is required")
    previous_runner = os.environ.get(WINDOWS_SANDBOX_EXE_ENV)
    previous_home = os.environ.get("KIROCREW_HOME")
    diagnostics: list[str] = []
    capabilities = {}
    try:
        capability_result = subprocess.run(
            [str(args.runner.absolute()), "--capabilities-json"],
            capture_output=True,
            check=False,
            timeout=5,
        )
        if capability_result.returncode == 0:
            capabilities = json.loads(capability_result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        diagnostics.append("capability-query-failed")
    try:
        with tempfile.TemporaryDirectory(prefix="kirocrew-windows-check-") as directory:
            os.environ[WINDOWS_SANDBOX_EXE_ENV] = str(args.runner.absolute())
            os.environ["KIROCREW_HOME"] = directory
            success = probe_windows_sandbox(diagnostics=diagnostics)
    finally:
        for key, previous in (
            (WINDOWS_SANDBOX_EXE_ENV, previous_runner),
            ("KIROCREW_HOME", previous_home),
        ):
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
    result = {
        "status": "passed" if success else "not_available",
        "windows_version": platform.version(),
        "runner": str(args.runner.absolute()),
        "diagnostics": diagnostics,
        "capabilities": capabilities,
        "scope": "synthetic filesystem and descendant confinement",
        "crew_installation_modified": False,
        "agent_started": False,
        "requires_acp_integration_validation": True,
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
