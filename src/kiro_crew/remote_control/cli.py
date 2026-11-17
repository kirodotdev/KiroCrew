"""``kirocrew remote-control [status|on|off]``: the terminal side of the setting.

``off`` writes an explicit ``false``, so Remote Control stays off across a restart.
A running gateway notices the write within a few seconds and stops the relay
(:mod:`kiro_crew.remote_control.guard`); no restart is needed for that. ``on``
takes effect when Kiro Crew restarts, and is refused (exit 1, nothing written) on a
build that locks the tunnel. ``status`` prints what the setting asks for.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

_ACTIONS = (
    ("status", "Show whether Remote Control is on"),
    ("on", "Turn personal Remote Control on; it connects after Kiro Crew restarts"),
    ("off", "Turn personal Remote Control off; it stays off across restarts"),
)

#: An LTS build ignores ``tunnel.relay_enabled`` (``edition_locks``), so no restart connects.
LOCKED_NOTICE = "Remote Control is managed by this build and cannot be turned on here."


def register_parser(sub: Any) -> None:
    from kiro_crew import cli_help

    parser = cli_help.add_command(sub, "remote-control")
    actions = parser.add_subparsers(dest="remote_control_action")
    for name, help_text in _ACTIONS:
        action = actions.add_parser(name, help=help_text)
        if name == "status":
            action.add_argument("--json", action="store_true", help="Print JSON")


def _audit(outcome: str, resources: str) -> None:
    from kiro_crew.sel import sel

    sel().log_api_access(
        caller="cli",
        operation="remote_control",
        outcome=outcome,
        source="cli",
        resources=resources,
    )


def _status(as_json: bool) -> int:
    from kiro_crew.remote_control import setting

    requested = setting.requested()
    locked = setting.locked()
    if as_json:
        print(json.dumps({"requested": requested, "locked": locked}))
        return 0
    if locked:
        print("Remote Control: off")
        print(LOCKED_NOTICE)
        return 0
    print(f"Remote Control: {'on' if requested else 'off'}")
    return 0


def run(args: argparse.Namespace) -> None:
    from kiro_crew.remote_control import setting

    action = getattr(args, "remote_control_action", None) or "status"
    if action == "status":
        sys.exit(_status(bool(getattr(args, "json", False))))
    if action == "off":
        setting.turn_off()
        _audit("allowed", "enabled=false")
        print("✅ Remote Control is off. It stays off until you turn it back on.")
        print("   A running gateway stops the relay within a few seconds.")
        return
    if setting.locked():
        # As the config PATCH's 409 locked_by_policy: saving it would do nothing.
        _audit("denied", "enabled=true reason=locked_by_policy")
        print(f"❌ {LOCKED_NOTICE}", file=sys.stderr)
        print("   Nothing was changed.", file=sys.stderr)
        sys.exit(1)
    setting.turn_on()
    _audit("allowed", "enabled=true")
    print("✅ Remote Control is on.")
    print("   It connects after Kiro Crew restarts.")
