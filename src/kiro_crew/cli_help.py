"""Grouped top-level help for the ``kirocrew`` CLI.

argparse lists subcommands as one flat block in registration order, which for
~40 commands reads as a wall the first three commands anybody needs are buried
in. This module owns the taxonomy instead: the ordered sections below are the
single source of truth for what the top-level help shows and in what order, so
``cli.py`` can keep registering a command wherever its arguments live.

``cli.py`` hides argparse's own listing (``help=SUPPRESS`` on the subparsers
action) and renders :func:`render_epilog` instead. Every user-facing command
MUST be registered through :func:`add_command`, which refuses a name that is
not in a section — that is what keeps the listing from silently omitting a new
command. Internal commands (the ``mcp-*`` servers the agent backend spawns)
call ``sub.add_parser`` directly with no ``help``, which keeps them out of both
listings.
"""

from __future__ import annotations

import argparse
from typing import Any, Iterable, Iterator, Mapping

# The dashboard port a default install binds. Duplicated as a STRING for help
# text only; the runtime value is config/loader.py's ``_DEFAULT_PORT``.
_DEFAULT_PORT_TEXT = "5476"

# Ordered sections -> ordered (command, one-line summary) pairs. Order here is
# display order; the first section is what a new user should read first.
COMMAND_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Start here",
        (
            ("gateway", "Start Kiro Crew in this terminal (dashboard + messaging channels)"),
            ("service", "Run the gateway as a background service that starts on boot"),
            ("doctor", "Verify this install and diagnose problems"),
        ),
    ),
    (
        "Run the gateway",
        (
            ("status", "Show runtime stats"),
            ("restart", "Restart a running gateway (service-aware)"),
            ("stop", "Stop a running gateway"),
            ("logs", "Show gateway logs"),
            ("token", "Print a dashboard access URL with auth token"),
            ("logout", "Revoke all active dashboard sessions"),
            ("update", "Update Kiro Crew to the latest version"),
        ),
    ),
    (
        "Set it up",
        (
            ("setup", "Install agent config and run the setup wizard"),
            ("config", "Get or set configuration values"),
            ("sandbox", "Manage the AppArmor profile the agent sandbox needs (Linux)"),
            ("manifest", "Generate a Slack app manifest with your alias"),
        ),
    ),
    (
        "Work with the agent",
        (
            ("chat", "Chat with the agent"),
            ("run", "Run an autonomous task from a spec file"),
            ("cron", "Manage scheduled jobs"),
            ("spawn", "Manage background subagents"),
            ("computer", "Computer-use (desktop automation) diagnostics"),
        ),
    ),
    (
        "Memory and knowledge",
        (
            ("memory", "Manage memory (vector store + markdown layer)"),
            ("knowledge", "Knowledge Base maintenance"),
            ("learn", "Save or manage learned corrections"),
            ("consolidate", "Force history consolidation (triggers skill extraction)"),
            ("artifact", "Manage saved artifacts (LLM-generated UI)"),
        ),
    ),
    (
        "Extend it",
        (
            ("app", "Manage Kiro Crew apps"),
            ("agent", "Manage Kiro Crew agent definitions"),
            ("workspace", "Manage workspace definitions"),
            ("project", "Create and register portable Project bundles"),
        ),
    ),
    (
        "Security and privacy",
        (
            ("secrets", "Migrate .env credentials into the encrypted secret vault"),
            ("security", "Security audit and deny list"),
            ("policy", "Inspect the governance security policy + profiles"),
            ("telemetry", "Inspect or disable anonymous usage telemetry"),
        ),
    ),
    (
        "Move it and back it up",
        (
            ("cloud", "Run Kiro Crew on your own AWS EC2 instance"),
            ("tailnet", "Publish this dashboard on your tailnet (Tailscale)"),
            ("snapshot", "Create a portable backup of Kiro Crew state"),
            ("restore", "Restore Kiro Crew state from a snapshot"),
        ),
    ),
    (
        "Develop Kiro Crew itself",
        (
            ("pod", "Isolated, throwaway, full-stack test instances per worktree"),
            ("eval", "Run multi-session evaluation scenarios"),
            ("bench", "Run external memory benchmarks (LongMemEval, LoCoMo)"),
            ("perf", "Debug-only performance sampling (off by default)"),
            ("desktop", "Debug-only desktop app diagnostics (requires KIROCREW_DEBUG)"),
        ),
    ),
)

SUMMARIES: dict[str, str] = {
    name: summary for _section, commands in COMMAND_GROUPS for name, summary in commands
}

# Position of each command in the help's listing, for ordering an error message
# the same way. ``sorted`` is stable, so anything absent sorts to the end while
# keeping its registration order.
_LISTING_RANK: dict[str, int] = {name: index for index, name in enumerate(SUMMARIES)}


def _listing_rank(name: str) -> int:
    return _LISTING_RANK.get(name, len(_LISTING_RANK))


# argparse builds the usage line from the actions it is allowed to show, and the
# subparsers action is hidden, so the placeholder is spelled out here instead.
TOP_USAGE = "kirocrew [-h] [--version] [-v] [--no-jail] <command> [<args>]"

# Why both commands exist, and what a default install actually listens on --
# the two questions the flat command list never answered.
_ORIENTATION = f"""\
gateway vs. service -- the same server, two lifetimes:
  kirocrew gateway          runs in the foreground and stops on Ctrl-C or when
                            the terminal closes. Best for a first look and for
                            development.
  kirocrew service install  registers a systemd unit (Linux, needs sudo) or a
                            launchd agent (macOS) that runs the SAME gateway
                            detached: it survives logout, restarts on crash and
                            starts at boot. Then use `kirocrew service status`,
                            `kirocrew restart`, `kirocrew logs`.
  Run only one of them at a time -- both bind the same port.

Ports: the dashboard is the only port Kiro Crew opens, and it binds loopback
  only -- http://localhost:{_DEFAULT_PORT_TEXT}. Messaging channels (Slack, Discord, ...)
  connect outbound, so nothing else needs to be reachable. Override the port
  with `kirocrew gateway --port N`, KIROCREW_PORT=N, or the `dashboard.url`
  config value; for the service, set KIROCREW_PORT when you run
  `service install` (later, edit /etc/kirocrew/kirocrew.env and restart)."""


def add_command(
    sub: argparse._SubParsersAction,  # type: ignore[type-arg]
    name: str,
    **kwargs: Any,
) -> argparse.ArgumentParser:
    """Register a user-facing top-level command.

    Passing ``help`` is pointless (``cli.py`` hides argparse's own listing), so
    the section summary is used as the subparser's ``description`` instead --
    which is what ``kirocrew <command> --help`` prints. A caller that wants a
    longer description just passes its own.

    Raises ``KeyError`` when ``name`` has no section, so a command cannot be
    added to the CLI without also appearing in the top-level help.
    """
    kwargs.setdefault("description", SUMMARIES[name])
    return sub.add_parser(name, **kwargs)


def render_epilog(width: int = 13) -> str:
    """Render the grouped command listing with the orientation notes inline.

    The notes sit directly under the first section rather than at the end: they
    answer "which of these two do I run, and what does it listen on" about the
    commands immediately above them, and a reader who stops after the first
    screen is exactly the reader who needs them.
    """
    lines: list[str] = []
    for index, (section, commands) in enumerate(COMMAND_GROUPS):
        lines.append(f"{section}:")
        for name, summary in commands:
            lines.append(f"  {name:<{width}}{summary}")
        lines.append("")
        if index == 0:
            lines.extend([_ORIENTATION, ""])
    lines.append("Run `kirocrew <command> -h` for a command's own options.")
    return "\n".join(lines)


def visible_commands(choices: Iterable[str]) -> list[str]:
    """The registered command names that belong in the top-level listing.

    ``mcp-*`` entries are MCP servers the agent backend spawns; they are not
    commands a person runs, so they are excluded here and carry no ``help``.
    """
    return [name for name in choices if not name.startswith("mcp-")]


class _VisibleCommandChoices(Mapping[str, Any]):
    """A live view of the subparser map that ITERATES only user-facing commands.

    argparse builds ``invalid choice: 'x' (choose from ...)`` by joining
    ``action.choices``, so an unknown command otherwise gets answered with all
    ~17 internal ``mcp-*`` server names — the same noise the listing itself was
    just cleaned of. Validation and dispatch do not read ``choices``: the parser
    tests membership (``__contains__``) and ``_SubParsersAction.__call__``
    resolves through its own ``_name_parser_map``. So membership stays complete
    while iteration is filtered, and ``kirocrew mcp-core`` keeps working.

    ``__len__`` follows ``__iter__`` (the visible count) to keep this a coherent
    view of what it claims to contain; nothing in argparse reads it.

    Iteration follows the help's own section order, so a typo is answered with
    ``gateway, service, doctor, ...`` rather than registration order. A command
    that somehow escaped the section table is still listed, at the end, because
    an error message must never omit a name the parser accepts.

    Backed by a REFERENCE to the real map rather than a copy, so a command
    registered after this view is installed is still recognised.
    """

    def __init__(self, commands: Mapping[str, Any]) -> None:
        self._commands = commands

    def __getitem__(self, key: str) -> Any:
        return self._commands[key]

    def __contains__(self, key: object) -> bool:
        return key in self._commands

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(visible_commands(self._commands), key=_listing_rank))

    def __len__(self) -> int:
        return len(visible_commands(self._commands))


def hide_internal_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Keep ``mcp-*`` out of argparse's invalid-choice error message."""
    # typeshed annotates ``choices`` as a concrete dict, but argparse only ever
    # iterates it (to build that message) and tests membership (to validate the
    # command), both of which a Mapping serves; dispatch reads the action's own
    # ``_name_parser_map``, which is left untouched.
    sub.choices = _VisibleCommandChoices(sub.choices)  # type: ignore[assignment]
