"""After closing Crew, prepare activation of its CURRENT saved Windows settings.

This never replays an unsaved request or changes keys, trust, or launch approvals.
It refuses while native readers or legacy governance pins remain live.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiro_crew import windows_settings  # noqa: E402
from kiro_crew.config import KiroCrewConfig  # noqa: E402


def main() -> int:
    if os.name != "nt":
        raise SystemExit("Recovery applies to native Windows only")
    with windows_settings.write_transaction():
        # Preflight every pin before a load can migrate a protected document.
        for name in windows_settings.DOCUMENTS:
            with windows_settings._delete_preflight(windows_settings._home() / name):
                pass
        cfg = KiroCrewConfig.load()
        result = windows_settings.claim_transition(
            {
                "status": "saved",
                "saved": True,
                "active": False,
                "starts_blocked": True,
                "transition_id": secrets.token_hex(16),
                "failed_step": None,
                "routing": sorted(cfg.mcp_gateway.stub_servers),
                "sharing": cfg.mcp_gateway.enabled,
                "documents": windows_settings.snapshot(),
                "action": "restart_and_review",
            }
        )
    print(
        json.dumps(
            {"status": result["status"], "action": "start_crew", "configuration_replayed": False}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
