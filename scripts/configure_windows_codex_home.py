"""Prepare Crew's private Windows Codex profile, optionally sign in independently."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiro_crew.acp.codex_home import (  # noqa: E402
    configure_codex_broker,
    prepare_codex_home,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", action="store_true")
    parser.add_argument(
        "--configure",
        action="store_true",
        help="Route Crew core/cron tools through the host broker",
    )
    parser.add_argument("--device-auth", action="store_true")
    parser.add_argument("--codex", default=os.environ.get("CODEX_PATH", "codex"))
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This setup is for native Windows Crew sessions")
    private = prepare_codex_home(os.environ, Path.home())
    if private is None:
        parser.error(
            "An explicit CODEX_HOME selects another profile; leave it unset for Crew isolation"
        )
    if args.configure:
        configure_codex_broker()
    print(
        json.dumps(
            {
                "codex_home": str(private),
                "desktop_history_modified": False,
                "private_auth_file_present": (private / "auth.json").exists(),
                "crew_core_and_cron_broker_configured": args.configure,
            }
        ),
        flush=True,
    )
    if not args.login:
        return 0
    command = [args.codex, "login"]
    if args.device_auth:
        command.append("--device-auth")
    return subprocess.run(
        command, env={**os.environ, "CODEX_HOME": str(private)}, check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
