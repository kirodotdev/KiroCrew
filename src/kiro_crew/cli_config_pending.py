"""``kirocrew config pending``: apply or discard a settings file changed outside Kiro Crew.

Where the sandbox seals the keystone files, a file whose on-disk bytes differ from its
endorsed copy is pending (``config.loosening_provenance.pending_changes``): it keeps
loading as the endorsed copy and its ordinary saves are refused until the owner decides.
Settings > Security is where the dashboard decides; this command is the same decision
for a gateway running without the dashboard (``--slack-only``). Apply and Discard run
the Settings routes' own writer (``dashboard.handlers.config_pending.resolve_pending``)
and name the version the owner was shown, so a later edit cannot ride the decision.

Owner only: no agent or MCP tool names it, and Apply and Discard refuse inside a Kiro
Crew sandbox, whose read-only bind on the endorsed copies refuses the write anyway.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import NoReturn

from kiro_crew import sandbox_launch_mark
from kiro_crew.config import loosening_provenance as lp
from kiro_crew.sel import sel


def pending_cmd(args: argparse.Namespace) -> None:
    action = getattr(args, "pending_action", None) or "list"
    if action == "list":
        _list()
    else:
        _resolve(args.file, apply=action == "apply", signature=args.signature)


def _list() -> None:
    pending = lp.pending_changes()
    if not pending:
        print("Nothing is held for review.")
    for entry in pending:
        leaf, signature = entry["leaf"], entry["signature"]
        if entry["removed"]:
            print(f"{leaf} was removed outside Kiro Crew; the saved version stays in effect.")
        elif not entry["disk_valid"]:
            print(
                f"{leaf} was changed outside Kiro Crew and is not valid JSON; "
                "it can only be discarded."
            )
        else:
            print(f"{leaf} was changed outside Kiro Crew; the saved version stays in effect.")
            for row in entry["changes"]:
                print(f"  {_row(row)}")
            if entry["truncated"]:
                print("  (more changes not shown)")
            print(f"  Apply:   kirocrew config pending apply {leaf} {signature}")
        print(f"  Discard: kirocrew config pending discard {leaf} {signature}")


def _row(row: dict) -> str:
    """One changed key, JSON-quoted so a key or value the edit wrote cannot drive the terminal."""
    line = f"{row['change']} {json.dumps('.'.join(row['path']))}"
    if row.get("redacted"):
        return f"{line} (value withheld)"
    values = [json.dumps(row[side]) for side in ("before", "after") if side in row]
    # The diff leaves out a side it cannot show; the other alone would misread the change.
    if row["change"] == "changed" and len(values) < 2:
        return line
    return f"{line}: {' -> '.join(values)}" if values else line


def _resolve(leaf: str, *, apply: bool, signature: str) -> None:
    from kiro_crew.config.loader import ConfigReadError, ConfigWriteRefused
    from kiro_crew.dashboard.handlers import config_pending
    from kiro_crew.dashboard.handlers.computer_use import StateCorruptError
    from kiro_crew.dashboard.handlers.security_audit import ConfigCorruptError

    op = config_pending.OP_APPLY if apply else config_pending.OP_DISCARD
    if sandbox_launch_mark.launched_in_sandbox():
        _refuse(
            op,
            f"{leaf}=sandboxed",
            "Run this from your own terminal: inside the agent sandbox a change to the "
            "files that confine the agent can be neither applied nor discarded.",
        )
    # Only a file listed at this version is resolved; this is also what limits the file
    # name to a protected leaf. Off the event loop, so the host check is the writer's.
    if not any(p["leaf"] == leaf and p["signature"] == signature for p in lp.pending_changes()):
        _refuse(op, f"{leaf}=changed", _not_pending(leaf))
    try:
        restart = asyncio.run(config_pending.resolve_pending(None, leaf, apply, signature))
    except lp.PendingChanged:
        _refuse(op, f"{leaf}=changed", _not_pending(leaf))
    except lp.PendingInvalid:
        _refuse(op, f"{leaf}=invalid", f"{leaf} is not valid JSON, so it cannot be applied.")
    except lp.PendingRemoved:
        _refuse(op, f"{leaf}=removed", f"{leaf} was removed; discard to put it back.")
    except ConfigWriteRefused as exc:
        _refuse(op, f"{leaf}=refused", f"{leaf} was not changed: {exc}")
    except (ConfigReadError, ConfigCorruptError, StateCorruptError, OSError) as exc:
        _refuse(op, f"{leaf}=unwritable", f"{leaf} could not be written: {exc}", outcome="error")
    _audit(op, "ok", leaf)
    if not apply:
        print(f"✅ Discarded the outside change to {leaf}; the saved version is back.")
        return
    print(f"✅ Applied the outside change to {leaf}.")
    if restart:
        print("Restart the gateway for a running instance to pick it up.")


def _not_pending(leaf: str) -> str:
    return (
        f"{leaf} is not held for review at that version. "
        "Run `kirocrew config pending` to see what is."
    )


def _refuse(op: str, resources: str, message: str, *, outcome: str = "denied") -> NoReturn:
    _audit(op, outcome, resources)
    print(f"❌ {message}", file=sys.stderr)
    sys.exit(1)


def _audit(op: str, outcome: str, resources: str) -> None:
    sel().log_api_access(
        caller="cli", operation=op, outcome=outcome, source="cli", resources=resources
    )
