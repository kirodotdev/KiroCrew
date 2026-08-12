"""Deploy script — kill gateway, rebuild backend + frontend, restart.

Usage:
    python scripts/deploy.py              # full deploy (kill + pip + npm + restart)
    python scripts/deploy.py --skip-frontend   # backend only
    python scripts/deploy.py --skip-restart    # build only, no restart
    python scripts/deploy.py --skip-kill       # don't kill gateway first (risky — pip may fail)
"""

import argparse
import subprocess
import sys
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PIP = ROOT / ".venv" / "Scripts" / "pip.exe"
WEBSITE = ROOT / "website"
STATIC_DIST = ROOT / "src" / "kiro_crew" / "static" / "dist"
TASK_NAME = "KiroCrewGateway"


def run(cmd: list[str], cwd: Path | None = None, label: str = "") -> None:
    print(f"\n{'='*60}")
    print(f"  {label or ' '.join(cmd)}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        print(f"\n✗ FAILED: {label or cmd[0]} (exit {result.returncode})")
        sys.exit(result.returncode)
    print(f"✓ {label or cmd[0]}")


def kill_gateway() -> None:
    """Kill all gateway processes so pip can overwrite the executable."""
    print("\n→ Killing gateway processes...")

    # Kill kiro-cli processes (so pip can overwrite kirocrew.exe)
    result = subprocess.run(
        ["taskkill", "/IM", "kirocrew.exe", "/F"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        killed = [l for l in result.stdout.splitlines() if "SUCCESS" in l]
        print(f"  Killed {len(killed)} kirocrew.exe process(es)")

    # Kill the gateway python process itself
    result = subprocess.run(
        ["wmic", "process", "where",
         "CommandLine like '%-m kiro_crew gateway%'",
         "delete"],
        capture_output=True, text=True,
    )
    if "deleted" in (result.stdout or "").lower() or "instance" in (result.stdout or "").lower():
        print("  Killed gateway python process(es)")
    else:
        print("  No running gateway python process found")

    # Kill any gateway wrapper cmd.exe (the bat loop)
    subprocess.run(
        ["wmic", "process", "where",
         "CommandLine like '%run-gateway.bat%'",
         "delete"],
        capture_output=True,
    )


def restart_gateway() -> None:
    """Restart via the scheduled task (runs hidden, no console window)."""
    print("\n→ Restarting gateway via scheduled task...")
    # Stop it first if running
    subprocess.run(
        ["schtasks", "/end", "/tn", TASK_NAME],
        capture_output=True,
    )
    result = subprocess.run(
        ["schtasks", "/run", "/tn", TASK_NAME],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"✓ {TASK_NAME} scheduled task triggered")
    else:
        print(f"⚠ Could not trigger scheduled task: {result.stderr.strip()}")
        print("  Start manually: schtasks /run /tn KiroCrewGateway")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy KiroCrew locally")
    parser.add_argument("--skip-frontend", action="store_true", help="Skip npm build")
    parser.add_argument("--skip-restart", action="store_true", help="Skip gateway restart")
    parser.add_argument("--skip-kill", action="store_true", help="Don't kill gateway before install")
    args = parser.parse_args()

    os.chdir(ROOT)

    # 1. Kill gateway (required so pip can overwrite kirocrew.exe)
    if not args.skip_kill:
        kill_gateway()

    # 2. Backend: pip install -e .
    run([str(VENV_PIP), "install", "-e", ".", "--quiet"], cwd=ROOT, label="pip install -e .")

    # 3. Frontend: npm run build + stage dist
    if not args.skip_frontend:
        npm = shutil.which("npm")
        if not npm:
            print("✗ npm not found on PATH")
            sys.exit(1)
        run([npm, "run", "build"], cwd=WEBSITE, label="npm run build")
        # Stage built frontend into the package's static dir
        if STATIC_DIST.exists():
            shutil.rmtree(STATIC_DIST)
        shutil.copytree(WEBSITE / "dist", STATIC_DIST)
        print(f"✓ Staged dist → {STATIC_DIST}")

    # 4. Restart gateway via scheduled task (hidden, no console window)
    if not args.skip_restart:
        restart_gateway()

    print("\n✓ Deploy complete.")


if __name__ == "__main__":
    main()
