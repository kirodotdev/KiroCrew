"""``kirocrew agent pin``: choose the spec file an agent name runs (``agent.spec_pins``).

``kirocrew agent pin <name> <file>`` sets the pin after checking that the file is a
spec in the user-level agents directory that declares ``<name>``;
``kirocrew agent pin <name> --clear`` removes it; with no arguments the pins are
listed, each with whether it still names a file that declares its agent, followed by
every name several specs declare. A pin applies to sessions started after it; a
running session's later spec reads follow the current pin.
"""

from __future__ import annotations

import argparse
import sys

from kiro_crew import agent_spec_pin_ops, agent_spec_pins
from kiro_crew.config.loader_document import ConfigReadError
from kiro_crew.terminal_safe import safe_terminal_line


def _lone_pin_state(name: str, file_name: str) -> str:
    """The state of a pin on a name at most one spec declares.

    The resolver refuses such a pin when it names no file that declares the name
    (whether one other file or none does), so it is ``broken`` then, like a
    contested one; a pin on the one claimant is ignored, shown as not contested.
    """
    try:
        agent_spec_pin_ops.check_pin_target(name, file_name, source="cli")
    except agent_spec_pin_ops.PinRefused as exc:
        return "unknown" if exc.code == "agents_dir_unreadable" else "broken"
    return "lone"


def _list_pins() -> None:
    pins = agent_spec_pins.spec_pins()
    try:
        contested = agent_spec_pin_ops.contested_agents(source="cli")
    except OSError as exc:
        print(f"Error: the agents directory cannot be read: {exc}", file=sys.stderr)
        sys.exit(1)
    states = {row["agent"]: row["pin_state"] for row in contested}
    if not pins:
        print("No agent spec pins.")
    for name in sorted(pins):
        state = states.get(name) or _lone_pin_state(name, pins[name])
        note = {
            "ok": "",
            "broken": "  (broken: names no file that declares it)",
            "unknown": "  (unknown: the agents directory cannot be read)",
        }.get(state, "  (not contested: at most one spec declares it)")
        print(f"{safe_terminal_line(name)} -> {safe_terminal_line(pins[name])}{note}")
    unpinned = [row for row in contested if row["pin"] is None and row["divergent"]]
    for row in unpinned:
        files = ", ".join(safe_terminal_line(c["file"]) for c in row["claimants"])
        print(f"Unpinned: {safe_terminal_line(row['agent'])} is declared by {files}")
        for claimant in row["claimants"]:
            print(f"  {agent_spec_pins.pin_command(row['agent'], claimant['file'])}")
        if row.get("omitted"):
            print(
                f"  ...and {row['omitted']} more file(s) not listed; pin one by its "
                f"file name: kirocrew agent pin {safe_terminal_line(row['agent'])} <file>"
            )


def agent_pin(args: argparse.Namespace) -> None:
    """Dispatch ``kirocrew agent pin``."""
    name = getattr(args, "name", None)
    file_name = getattr(args, "file", None)
    clear = bool(getattr(args, "clear", False))
    if name is None:
        if clear or file_name is not None:
            print("Usage: kirocrew agent pin [<name> (<file> | --clear)]", file=sys.stderr)
            sys.exit(2)
        _list_pins()
        return
    if clear == (file_name is not None):
        print(
            "Usage: kirocrew agent pin <name> <file>  or  kirocrew agent pin <name> --clear",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        if not clear:
            assert file_name is not None
            agent_spec_pin_ops.check_pin_target(name, file_name, source="cli")
        existed = agent_spec_pin_ops.write_spec_pin(name, None if clear else file_name)
    except agent_spec_pin_ops.PinRefused as exc:
        print(f"Error: {safe_terminal_line(str(exc))}", file=sys.stderr)
        sys.exit(1)
    except (OSError, ConfigReadError) as exc:
        print(f"Error: could not write config.json: {exc}", file=sys.stderr)
        sys.exit(1)
    # A pin decides which spec, with its tools and prompt, an agent's sessions run:
    # recorded like the dashboard's write of it.
    from kiro_crew.sel import sel

    agent_spec_pin_ops.audit_pin_write(
        sel().log_api_access,
        caller="cli",
        source="cli",
        agent=name,
        file_name=None if clear else file_name,
    )
    if clear:
        if existed:
            print(f"Cleared the spec pin for {safe_terminal_line(name)}.")
        else:
            print(f"{safe_terminal_line(name)} had no spec pin.")
        return
    print(f"Pinned {safe_terminal_line(name)} to {safe_terminal_line(file_name or '')}.")
    print("   It applies to sessions started from now on; restart a running session to use it.")
