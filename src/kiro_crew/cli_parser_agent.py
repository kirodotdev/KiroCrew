"""Argparse definitions for the ``Work with the agent``, ``Memory and knowledge`` and
``Extend it`` commands.

One registrar per command of those ``cli_help.COMMAND_GROUPS`` sections: chat, run, cron,
spawn, computer, memory, knowledge, learn, consolidate, artifact, app, agent and workspace.
``kiro_crew.cli.main`` builds the top-level parser and calls every registrar in the command
line's registration order, so a new command of these sections gets a registrar here and a
call there.

Parser construction only: the handlers live where ``main`` dispatches them, and nothing
here reads a name a test patches through ``kiro_crew.cli``
(``test/test_cli_split_facade.py`` pins that).
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from kiro_crew import cli_help
from kiro_crew.cli_arg_types import non_negative_int, positive_int
from kiro_crew.cron_service.model import (
    _ALERT_AFTER_DEFAULT,
    _AUTO_PAUSE_MAX,
    _AUTO_PAUSE_THRESHOLD,
)

if TYPE_CHECKING:
    _Commands = argparse._SubParsersAction[argparse.ArgumentParser]

# ``--auto-pause-after`` help for ``cron add`` and ``cron update``. The cap and
# the default are read from the cron model so this copy cannot drift from them.
_AUTO_PAUSE_AFTER_HELP = (
    f"Consecutive failed runs before the job auto-pauses (0..{_AUTO_PAUSE_MAX}, "
    f"default {_AUTO_PAUSE_THRESHOLD}; 0 = never auto-pause. With 0 or a limit "
    f"above {_AUTO_PAUSE_THRESHOLD}, from the {_AUTO_PAUSE_THRESHOLD}th consecutive "
    "failure each further run waits 5 minutes, doubling per failure up to "
    "one hour, until it succeeds or reaches a nonzero limit)"
)
_ALERT_AFTER_HELP = (
    f"Consecutive failed runs before the failure message is sent (1..{_AUTO_PAUSE_MAX}, "
    f"default {_ALERT_AFTER_DEFAULT}). The dashboard bell still records every failure; "
    "policy denials and the run that auto-pauses the job always alert"
)

# Helper for commands with examples
_fmt = argparse.RawDescriptionHelpFormatter


def register_chat_parser(sub: _Commands, _jail_opts: argparse.ArgumentParser) -> None:
    chat_parser = cli_help.add_command(
        sub,
        "chat",
        epilog="""
Examples:
  kirocrew chat                      # Interactive mode
  kirocrew chat -m 'check my CRs'    # Single message
  kirocrew chat --model claude-opus  # Use specific model

Tool permission prompt (same keys as kiro-cli chat):
  y  allow this call once
  n  deny (default; anything else also denies)
  o  deny and type a reply to the agent instead
""",
        formatter_class=_fmt,
        parents=[_jail_opts],
    )
    chat_parser.add_argument("-m", "--message", help="Single message (non-interactive)")
    chat_parser.add_argument("--model", help="Model to use (default: from config)")
    chat_parser.add_argument("--agent", help="Agent to use (default: from config)")


def register_cron_parser(sub: _Commands) -> None:
    cron_parser = cli_help.add_command(
        sub,
        "cron",
        epilog="""
Examples:
  kirocrew cron list
  kirocrew cron add 'daily-status' 'show status' --every 86400
  kirocrew cron add 'weekday-9am' 'check tickets' --cron '0 9 * * MON-FRI' --approval-mode auto
  kirocrew cron add 'c360-check' 'check pipeline' --every 600 --agent customer360-code-agent
  kirocrew cron update <job-id> --approval-mode auto
  kirocrew cron update <job-id> --agent oncall-agent
  kirocrew cron remove <job-id>
""",
        formatter_class=_fmt,
    )
    cron_sub = cron_parser.add_subparsers(dest="cron_action")
    cron_sub.add_parser("list", help="List cron jobs")
    cron_add = cron_sub.add_parser(
        "add",
        help="Add a cron job",
        description="Add a cron job. The job is persisted in one locked write and its id is "
        "printed on stdout; any refusal exits non-zero (2 for an argparse usage error such "
        "as two mutually exclusive flags, 1 for every refusal the command itself raises: "
        "a missing schedule, a validation, security or store refusal), so an installer "
        "can register a job headlessly and detect a refused one.",
        epilog="""
Examples:
  kirocrew cron add "standup" "post the standup" --cron "0 9 * * MON-FRI" --timezone Europe/Paris
  kirocrew cron add "version-check" "" --every 3600 --script ~/.kiro/crew/crons/check.py:run \\
      --no-persistent-session --minimal-context
  kirocrew cron add "disk" "" --every 600 --command "df -h /" --timeout 30 --timeout-secs 60
  kirocrew cron add "reminder" "call the vet" --at "tomorrow 9am"
""",
        formatter_class=_fmt,
    )
    cron_add.add_argument("name", help="Job name")
    cron_add.add_argument(
        "message",
        help="Message to send to the agent. For a --script job this is ctx.message; "
        "for a --command job it is recorded but not used.",
    )
    cron_add_schedule = cron_add.add_mutually_exclusive_group()
    cron_add_schedule.add_argument("--every", type=int, help="Interval in seconds")
    cron_add_schedule.add_argument(
        "--cron", dest="cron_expr", help='Cron expression (e.g. "0 9 * * MON-FRI")'
    )
    cron_add_schedule.add_argument(
        "--at",
        dest="at",
        help="One-shot: fire once at this time and then delete the job. A Unix timestamp, "
        "or a time string ('5pm', 'in 30 minutes', 'tomorrow 9am', '2026-10-01 09:00') "
        "read in --timezone, or in the configured timezone when --timezone is omitted.",
    )
    cron_add.add_argument(
        "--timezone",
        dest="timezone",
        default="",
        help="IANA timezone the job's wall clock is read in (e.g. America/New_York): the "
        "--cron expression's fields, or an --at time string such as '9am'. Defaults to the "
        "configured timezone; refused with --every (an interval has no wall clock).",
    )
    cron_add.add_argument("--channel", help="Slack channel ID to post results to")
    # One job KIND per job. A script or command job never launches an agent,
    # so --agent alongside either would be dead configuration; argparse refuses
    # the pair up front instead of persisting a field nothing reads.
    cron_add_kind = cron_add.add_mutually_exclusive_group()
    cron_add_kind.add_argument(
        "--agent",
        dest="agent",
        default="",
        help="Agent name for this job (e.g. 'customer360-code-agent'). "
        "Empty or omitted uses agent.default_agent, or the kirocrew agent "
        "when that is unset or not eligible (e.g. a default that restricts "
        "subagents).",
    )
    cron_add_kind.add_argument(
        "--script",
        dest="script",
        default="",
        metavar="FILE.py:FUNC",
        help="Zero-token job: run this Python function instead of prompting an agent. "
        "The file must ALREADY be under ~/.kiro/crew/crons/ (this command registers, "
        "it does not copy) and is security-scanned before the job is stored.",
    )
    cron_add_kind.add_argument(
        "--command",
        # NOT dest="command": that is the top-level subcommand's dest, and a
        # subparser default silently overwrites the parent's parsed value (the
        # same trap the --no-jail comment in cli.main documents) -- `kirocrew cron add` would then
        # dispatch as command="" and print the top-level help.
        dest="shell_command",
        default="",
        metavar="SHELL",
        help="Zero-token job: run this shell command (sh -c, sandboxed) instead of "
        "prompting an agent. Vetted by the same deny-list as the bash tool.",
    )
    cron_add.add_argument(
        "--timeout",
        type=int,
        dest="timeout",
        default=None,
        help="Subprocess timeout in seconds for a --script/--command job "
        "(0..3600; omit for the store's per-kind default)",
    )
    cron_add.add_argument(
        "--timeout-secs",
        type=int,
        dest="timeout_secs",
        default=None,
        help="Per-wake execution budget in seconds (1..86400, default 1800). For a "
        "--script/--command job it must cover the subprocess timeout -- --timeout, or "
        "the store's per-kind default when --timeout is omitted -- plus 5s cleanup.",
    )
    cron_add.add_argument(
        "--auto-pause-after",
        type=int,
        dest="auto_pause_after_failures",
        default=None,
        help=_AUTO_PAUSE_AFTER_HELP,
    )
    cron_add.add_argument(
        "--alert-after",
        type=int,
        dest="alert_after_failures",
        default=None,
        help=_ALERT_AFTER_HELP,
    )
    cron_add.add_argument(
        "--model",
        dest="model",
        default="",
        help="Model id for the agent wake (as advertised by kiro-cli --list-models)",
    )
    cron_add.add_argument(
        "--persistent-session",
        dest="persistent_session",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resume one long-lived session across wakes (default on). Pass "
        "--no-persistent-session for a --script/--command job: it has no conversation "
        "to resume.",
    )
    cron_add.add_argument(
        "--minimal-context",
        dest="minimal_context",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Wake with a ~200-token context instead of the full memory/lessons/steering "
        "load (default off). Recommended for a --script/--command job.",
    )
    cron_add.add_argument(
        "--hide-in-chat",
        dest="hide_in_chat",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Keep the job's wakes out of the chat sidebar",
    )
    cron_add.add_argument(
        "--silent",
        action="store_true",
        help="Suppress auto-delivery; agent controls notifications",
    )
    cron_add.add_argument(
        "--folder",
        default="",
        help="Schedule-page folder to file the job in (existing folder name or id). "
        "The CLI does not create folders — create them in the dashboard's Schedule page.",
    )
    cron_add.add_argument(
        "--approval-mode",
        dest="approval_mode",
        choices=["auto"],
        default="",
        help='Tool approval mode ("auto" to auto-approve all tools)',
    )
    cron_add.add_argument(
        "--managed-by",
        dest="managed_by",
        default=None,
        metavar="KEY",
        help="Install-time owner key for an external installer (e.g. "
        "'mysystem:owner/asset-name'). Unique across the store: a second add with the "
        "same key is refused. Re-install with 'cron remove --managed-by KEY' and then "
        "add again.",
    )
    cron_update = cron_sub.add_parser("update", help="Update a cron job")
    cron_update.add_argument("job_id", help="Job ID to update")
    cron_update.add_argument("--name", help="New job name")
    cron_update.add_argument("--message", help="New message")
    cron_update.add_argument("--every", type=int, dest="every_secs", help="New interval in seconds")
    cron_update.add_argument(
        "--timeout-secs",
        type=int,
        dest="timeout_secs",
        default=None,
        help="Per-wake execution budget in seconds (1..86400, default 1800)",
    )
    cron_update.add_argument(
        "--auto-pause-after",
        type=int,
        dest="auto_pause_after_failures",
        default=None,
        help=_AUTO_PAUSE_AFTER_HELP,
    )
    cron_update.add_argument(
        "--alert-after",
        type=int,
        dest="alert_after_failures",
        default=None,
        help=_ALERT_AFTER_HELP,
    )
    cron_update.add_argument("--cron", dest="cron_expr", help="New cron expression")
    cron_update.add_argument("--channel", help="New channel ID")
    cron_update.add_argument(
        "--agent",
        dest="agent",
        default=None,
        help="New agent name (empty string resets to agent.default_agent, "
        "or the kirocrew agent when that is unset or not eligible)",
    )
    cron_update.add_argument(
        "--approval-mode",
        dest="approval_mode",
        choices=["auto", "default"],
        default=None,
        help='Tool approval mode ("auto" to auto-approve, "default" to reset)',
    )
    cron_rm = cron_sub.add_parser("remove", help="Remove a cron job")
    cron_rm_target = cron_rm.add_mutually_exclusive_group(required=True)
    cron_rm_target.add_argument("job_id", nargs="?", default=None, help="Job ID to remove")
    cron_rm_target.add_argument(
        "--managed-by",
        dest="managed_by",
        default=None,
        metavar="KEY",
        help="Remove the job registered with 'cron add --managed-by KEY'",
    )
    cron_pause = cron_sub.add_parser("pause", help="Pause a cron job")
    cron_pause.add_argument("job_id", help="Job ID to pause")
    cron_resume = cron_sub.add_parser("resume", help="Resume a cron job")
    cron_resume.add_argument("job_id", help="Job ID to resume")
    cron_trigger = cron_sub.add_parser("trigger", help="Trigger a cron job immediately")
    cron_trigger.add_argument("job_id", help="Job ID to trigger")

    cron_adopt = cron_sub.add_parser(
        "adopt",
        help="Give a cron job an owning chat session, so that session can manage it and receives its results",
    )
    cron_adopt.add_argument("job_id", help="Job ID to adopt")
    adopt_target = cron_adopt.add_mutually_exclusive_group(required=True)
    adopt_target.add_argument(
        "--session-of",
        metavar="SESSION",
        help='Dashboard slot name ("chat-3-1712793600") or a fully-qualified '
        'session key ("dashboard:chat-3-1712793600"); see `kirocrew cron list`',
    )
    adopt_target.add_argument(
        "--release",
        action="store_true",
        help="Clear the owning session, returning the job to CLI/dashboard-only management",
    )

    cron_preview = cron_sub.add_parser(
        "preview",
        help="Run a script cron locally with real MCP tools; notifications are captured and printed instead of delivered",
    )
    cron_preview.add_argument(
        "script", help="Script file in file.py:function format (e.g. ~/.kiro/crew/crons/my.py:run)"
    )
    cron_preview.add_argument("--message", "-m", default="", help="ctx.message value")
    cron_preview.add_argument(
        "--env", "-e", action="append", metavar="K=V", help="Extra env vars (repeatable)"
    )


def register_spawn_parser(sub: _Commands) -> None:
    spawn_parser = cli_help.add_command(
        sub,
        "spawn",
        epilog="""
Examples:
  kirocrew spawn run 'check my open CRs'        # Wait for result
  kirocrew spawn run --async 'analyze logs'     # Fire-and-forget
  kirocrew spawn run --crew ops 'triage alerts' # Run as crew member 'ops'
  kirocrew spawn run --agent reviewer 'review'  # Run under agent 'reviewer'
  kirocrew spawn run --model MODEL_ID 'task'    # Run on model MODEL_ID
  kirocrew spawn list                           # Show active subagents
""",
        formatter_class=_fmt,
    )
    spawn_sub = spawn_parser.add_subparsers(dest="spawn_action")
    spawn_run = spawn_sub.add_parser("run", help="Spawn a subagent")
    spawn_run.add_argument("task", help="Task for the subagent")
    spawn_run.add_argument(
        "--async",
        dest="fire_and_forget",
        action="store_true",
        help="Fire-and-forget (don't wait for result)",
    )
    spawn_run.add_argument(
        "--agent",
        metavar="NAME",
        help="Run under this agent template; an unknown name is refused, never replaced",
    )
    spawn_run.add_argument(
        "--crew",
        metavar="NAME",
        help=(
            "Run as this crew member, with its memory store and template; "
            "an unknown member is refused, never replaced"
        ),
    )
    spawn_run.add_argument(
        "--model",
        metavar="ID",
        help="Run on this model id; it outranks the member's and the template's pin",
    )
    spawn_sub.add_parser("list", help="List subagents")
    spawn_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port (default: found as `kirocrew status` finds it)",
    )


def register_run_parser(sub: _Commands, _jail_opts: argparse.ArgumentParser) -> None:
    # run (autonomous task runner)
    run_parser = cli_help.add_command(
        sub,
        "run",
        epilog="""
Examples:
  kirocrew run TASK.md                  # Run task with auto-resume
  kirocrew run TASK.md --fresh          # Start from scratch
  kirocrew run TASK.md --no-test        # Skip test verification
  kirocrew run TASK.md --timeout 3600   # 1 hour timeout
""",
        formatter_class=_fmt,
        parents=[_jail_opts],
    )
    run_parser.add_argument("spec", help="Path to the spec/task file (e.g. TASK.md)")
    run_parser.add_argument(
        "--name",
        default="",
        help="Human-readable task name (auto-derived from spec if omitted)",
    )
    run_parser.add_argument(
        "--no-test",
        action="store_true",
        help="Skip running build/test verification after each step",
    )
    run_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore checkpoint, start task from scratch",
    )
    run_parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="Global timeout in seconds (0 = no limit)",
    )
    # Nothing in `kirocrew run` reads --port: the task runner reports to this
    # terminal, never to a dashboard. It is still accepted so a script that
    # passes it keeps parsing, and help does not offer it.
    run_parser.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)


def register_knowledge_parser(sub: _Commands) -> None:
    kn_parser = cli_help.add_command(sub, "knowledge")
    kn_sub = kn_parser.add_subparsers(dest="knowledge_action")
    kn_dedup = kn_sub.add_parser(
        "dedup", help="Collapse cross-source duplicate documents (dry-run unless --apply)"
    )
    kn_dedup.add_argument(
        "--apply",
        action="store_true",
        help="Apply the deletions (default: dry-run preview that changes nothing)",
    )
    kn_stats = kn_sub.add_parser(
        "stats", help="Count sources, documents and items in the knowledge library (read-only)"
    )
    kn_stats.add_argument(
        "--json", action="store_true", help="Emit the counts as JSON instead of a table"
    )


def register_consolidate_parser(sub: _Commands, _jail_opts: argparse.ArgumentParser) -> None:
    consolidate_parser = cli_help.add_command(sub, "consolidate", parents=[_jail_opts])
    consolidate_parser.add_argument(
        "session_key",
        nargs="?",
        default=None,
        help="Session key to consolidate (omit to list pending sessions)",
    )
    consolidate_parser.add_argument(
        "--all",
        action="store_true",
        dest="consolidate_all",
        help="Consolidate all sessions with unconsolidated messages",
    )


def register_computer_parser(sub: _Commands) -> None:
    # them in the ``kirocrew-computer`` MCP server instead.
    computer_parser = cli_help.add_command(
        sub,
        "computer",
        epilog="""
Examples:
  kirocrew computer doctor                     # Support + permission report
  kirocrew computer doctor --json              # The same report as JSON
  kirocrew computer apps                       # Apps with an on-screen window

Computer use is OFF by default and is enabled only from the dashboard
(Settings -> Computer Use). An agent cannot enable it.
""",
        formatter_class=_fmt,
    )
    computer_parser.add_argument(
        "computer_args",
        nargs=argparse.REMAINDER,
        help="computer sub-command and its arguments",
    )


def register_learn_parser(sub: _Commands) -> None:
    learn_parser = cli_help.add_command(
        sub,
        "learn",
        epilog="""
Examples:
  kirocrew learn list
  kirocrew learn add 'use snake_case for variables' --category tool
  kirocrew learn remove 'snake_case'
""",
        formatter_class=_fmt,
    )
    learn_sub = learn_parser.add_subparsers(dest="learn_action")
    learn_add = learn_sub.add_parser("add", help="Save a lesson")
    learn_add.add_argument("rule", help="The rule or correction to remember")
    learn_add.add_argument(
        "--category",
        choices=["tool", "preference", "knowledge"],
        default="knowledge",
        help="Lesson category (default: knowledge)",
    )
    learn_add.add_argument("--negative", help="What NOT to do (optional)")
    learn_sub.add_parser("list", help="List all lessons")
    learn_rm = learn_sub.add_parser("remove", help="Remove lessons matching a substring")
    learn_rm.add_argument("query", help="Substring to match against lesson rules")
    learn_rm.add_argument(
        "--repo-scope",
        dest="repo_scope",
        default=None,
        help=(
            "Only remove lessons carrying this repo scope. A lesson's "
            "identity is (rule, repo_scope), so a scoped and a global lesson can "
            "share rule text; without this flag a matching substring removes both. "
            "Omit to match every scope (the default). Pass an empty string to "
            'target only the unscoped (global) rows: --repo-scope "".'
        ),
    )


def register_artifact_parser(sub: _Commands) -> None:
    art_parser = cli_help.add_command(
        sub,
        "artifact",
        epilog="""
Examples:
  kirocrew artifact list
  kirocrew artifact list --tag op --kind widget
  kirocrew artifact save --name "CR Queue" --content-file widget.html --tags ops,cr
  cat widget.html | kirocrew artifact save --name "Pipeline Health"
  kirocrew artifact show cr-queue
  kirocrew artifact show cr-queue --version 2
  kirocrew artifact show cr-queue --meta
  kirocrew artifact update cr-queue --content-file widget.html
  kirocrew artifact versions cr-queue
  kirocrew artifact delete cr-queue
""",
        formatter_class=_fmt,
    )
    art_sub = art_parser.add_subparsers(dest="artifact_action")

    art_list = art_sub.add_parser("list", help="List saved artifacts")
    art_list.add_argument("--tag", help="Filter by tag")
    art_list.add_argument(
        "--kind",
        choices=["widget", "html", "markdown", "svg", "json", "text", "image"],
        help="Filter by kind",
    )
    art_list.add_argument("-q", "--q", help="Substring filter on artifact name")

    art_show = art_sub.add_parser("show", help="Print an artifact's content")
    art_show.add_argument("slug", help="Artifact slug")
    art_show.add_argument(
        "--version", type=positive_int, help="Specific version, 1 or more (default: current)"
    )
    art_show.add_argument(
        "--meta",
        action="store_true",
        help="Print metadata as JSON instead of the content body",
    )

    art_save = art_sub.add_parser("save", help="Save a new artifact")
    art_save.add_argument("--name", required=True, help="Human-readable name")
    art_save.add_argument(
        "--slug",
        help=(
            "Explicit slug instead of one derived from --name. A taken or "
            "malformed slug is REFUSED, never renamed: use 'artifact update "
            "<slug>' to version one in place. Omit to let a collision resolve "
            "by suffixing (-2, -3, ...)"
        ),
    )
    art_save.add_argument(
        "--kind",
        choices=["widget", "html", "markdown", "svg", "json", "text", "image"],
        default=None,
        help="Artifact kind (default: inferred from content / file extension)",
    )
    art_save.add_argument("--content", help="Inline content")
    art_save.add_argument("--content-file", help="Path to file containing the content")
    art_save.add_argument("--description", default="", help="Short description")
    art_save.add_argument("--tags", help="Comma-separated tag list")

    art_update = art_sub.add_parser("update", help="Update an artifact in place")
    art_update.add_argument("slug", help="Artifact slug to update")
    art_update.add_argument("--content", help="Inline new content")
    art_update.add_argument("--content-file", help="Path to file containing new content")
    art_update.add_argument("--name", help="New name (rename)")
    art_update.add_argument("--description", help="New description")
    art_update.add_argument("--tags", help="Replacement tag list (comma-separated)")

    art_del = art_sub.add_parser("delete", help="Delete an artifact and all its versions")
    art_del.add_argument("slug", help="Artifact slug to delete")

    art_ver = art_sub.add_parser("versions", help="List the version numbers for an artifact")
    art_ver.add_argument("slug", help="Artifact slug")


def register_memory_parser(sub: _Commands) -> None:
    mem_parser = cli_help.add_command(sub, "memory")
    mem_sub = mem_parser.add_subparsers(dest="mem_action")
    mem_sub.add_parser("list", help="Show semantic memory entries")
    mem_search = mem_sub.add_parser("search", help="Search memory (vector + markdown layers)")
    mem_search.add_argument("query", help="Search query text")
    mem_search.add_argument(
        "--layer",
        choices=["vector", "history", "all"],
        default="all",
        help=(
            "Which memory to search: vector (episodic semantic recall), "
            "history (keyword FTS over preferences/projects/daily history), "
            "or all (default)"
        ),
    )
    mem_show = mem_sub.add_parser(
        "show", help="Show the markdown memory layer (preferences, projects, daily history)"
    )
    mem_show.add_argument(
        "target",
        nargs="?",
        choices=["preferences", "projects", "history"],
        help="Layer to show (default: all three)",
    )
    mem_show.add_argument(
        "--format",
        choices=["md", "json"],
        default="md",
        help="Output format: raw markdown or structured JSON (default: md)",
    )
    mem_show.add_argument(
        "--since",
        help="History only: include days on or after DATE (YYYY-MM-DD)",
    )
    mem_sub.add_parser("stats", help="Show memory statistics")
    mem_sub.add_parser("audit", help="Scan memory for suspicious content")
    mem_export = mem_sub.add_parser("export", help="Export one memory store's rows to JSON")
    mem_export.add_argument("--output", "-o", help="Output file (default: stdout)")
    # Named for the same reason `backups`, `restore` and `carve` are: a store is a
    # separate on-disk silo, so "all memory" was never a thing one file held. Without
    # this flag no surface could read a named store's rows in either direction.
    mem_export.add_argument(
        "--store", default=None, help="Store to export (default: the default store)"
    )
    mem_export.add_argument(
        "--include-markdown",
        action="store_true",
        help="Also include the markdown layer (preferences, projects, daily history)",
    )
    mem_sub.add_parser("migrate", help="Migrate legacy markdown memory to vector store")
    mem_backup = mem_sub.add_parser("backup", help="Back up active memory stores now")
    mem_backup.add_argument(
        "--keep", type=positive_int, default=None, help="How many backups to keep per store"
    )
    mem_backups = mem_sub.add_parser("backups", help="List memory backups, newest first")
    mem_backups.add_argument("--store", default=None, help="Only this store")
    mem_restore = mem_sub.add_parser("restore", help="Restore a memory store from a backup")
    mem_restore.add_argument(
        "--store", default=None, help="Store to restore (default: the default store)"
    )
    mem_restore_choice = mem_restore.add_mutually_exclusive_group()
    mem_restore_choice.add_argument(
        "--from",
        dest="from_backup",
        default=None,
        help="Backup file to restore (default: the newest for that store)",
    )
    mem_restore_choice.add_argument(
        "--cancel-pending",
        action="store_true",
        help="Cancel a staged memory restore without changing active memory",
    )
    mem_create = mem_sub.add_parser(
        "create-store",
        help="Create a declared memory store's directory (use never creates it)",
        description=(
            "Create the directory of a named V1 memory store declared in config.json "
            "memory_stores. A store is never created on first use, so a deleted store "
            "stays missing until this is run. Member stores are created with their member."
        ),
    )
    mem_create.add_argument("name", help="The declared store's name")
    mem_carve = mem_sub.add_parser(
        "carve", help="Filter or count a crew store's memory by its carve facets"
    )
    mem_carve.add_argument(
        "--store",
        default=None,
        help="Memory store to read (default: the default store, which carries no facets)",
    )
    # One flag per field of memory_schema.MemoryFacets, and each argparse dest IS the
    # column name, so `_memory_carve` reads them by facet name instead of restating the
    # list. The flag spellings and the two closed choice sets are LITERAL here rather
    # than derived from memory_schema: this parser is built on `kirocrew gateway`'s
    # boot path, where an import of the schema module is work every launch pays for
    # a verb it never runs. Drift is caught instead, not prevented -- the rendered
    # `memory carve --help` is checked against memory_schema.ALL_KINDS and
    # GROUPABLE_COLUMNS, so a sixth axis added to the dataclass without a flag or a
    # choice here fails that check rather than silently going unfilterable.
    mem_carve.add_argument("--scope", default=None, help="Only rows carved to this repo scope")
    mem_carve.add_argument("--surface", default=None, help="Only rows from this surface")
    mem_carve.add_argument("--crew", default=None, help="Only rows this crew produced")
    mem_carve.add_argument("--session-key", default=None, help="Only rows from this conversation")
    mem_carve.add_argument(
        "--derived-from", default=None, help="Only rows synthesized from this item id"
    )
    mem_carve.add_argument(
        "--kind",
        default=None,
        choices=["directive", "fact", "episode"],
        help="Only rows of this kind",
    )
    mem_carve.add_argument(
        "--count-by",
        default=None,
        choices=["scope", "surface", "crew", "session_key", "derived_from", "kind"],
        help="Report counts grouped by this axis instead of listing rows",
    )
    mem_carve.add_argument("--limit", type=positive_int, default=50, help="How many rows to list")
    mem_carve.add_argument(
        "--offset", type=non_negative_int, default=0, help="Rows to skip when listing"
    )
    mem_retired = mem_sub.add_parser(
        "retired", help="List episodes a semantic write superseded, and restore one"
    )
    mem_retired.add_argument("--restore", dest="restore_id", default=None, help="Restore this id")
    mem_retired.add_argument(
        "--limit", type=positive_int, default=20, help="How many to list (default: 20)"
    )
    mem_forget = mem_sub.add_parser(
        "forget",
        help="Delete one semantic memory key after you confirm it (owner only; "
        "needs a terminal and the running gateway)",
    )
    mem_forget.add_argument("key", help="Semantic memory key to forget")
    mem_forget.add_argument(
        "--superseded-by",
        dest="superseded_by",
        default=None,
        help="Live key that replaces this one; recorded on the tombstone",
    )
    mem_import = mem_sub.add_parser("import", help="Import memory from JSON file")
    mem_import.add_argument("file", help="Path to JSON file (export format)")
    mem_import.add_argument(
        "--store", default=None, help="Store to import into (default: the default store)"
    )


def register_agent_parser(sub: _Commands) -> None:
    agent_parser = cli_help.add_command(sub, "agent")
    agent_sub = agent_parser.add_subparsers(dest="agent_action")
    agent_sub.add_parser("list", help="List Kiro Crew agents")
    agent_create = agent_sub.add_parser("create", help="Create a Kiro Crew agent")
    agent_create.add_argument(
        "--name",
        required=True,
        help="Agent id, or any name: shown as typed, stored under a URL-safe id",
    )
    agent_create.add_argument("--display-name", default="", help="Label the dashboard shows")
    agent_create.add_argument(
        "--kiro-agent",
        default="kirocrew",
        help=(
            "Template the crew starts from. Only 'kirocrew', the built-in worker "
            "(the default), is accepted; add other abilities in the crew's editor"
        ),
    )
    agent_create.add_argument(
        "--workspace",
        default="",
        help=(
            "Share an existing workspace by name; omitted, the member gets a workspace "
            "of its own (member-<slug>)"
        ),
    )
    agent_create.add_argument(
        "--memory-store",
        default="default",
        help="Create a fresh member store (already enabled for explicit member creation)",
    )
    agent_create.add_argument(
        "--model",
        default=None,
        help=(
            "Default model for the agent's sessions; omitted or 'auto', it follows "
            "the template's pinned model, then agent.model"
        ),
    )
    agent_update = agent_sub.add_parser("update", help="Update a Kiro Crew agent")
    agent_update.add_argument("name", help="Agent name to update")
    agent_update.add_argument("--kiro-agent", help="New kiro agent name")
    agent_update.add_argument("--workspace", help="New workspace name")
    agent_update.add_argument(
        "--display-name",
        help='New label the dashboard shows; "" clears it back to the agent name',
    )
    agent_update.add_argument(
        "--memory-store",
        help=(
            "Existing memory store identity (cannot be changed, except to 'default' "
            "from a store name the config refuses)"
        ),
    )
    agent_update.add_argument(
        "--model",
        help=(
            "New default model for the agent's sessions; \"\" or 'auto' clears the pin "
            "so it follows the template's pinned model, then agent.model"
        ),
    )
    agent_delete = agent_sub.add_parser("delete", help="Delete a Kiro Crew agent")
    agent_delete.add_argument("name", help="Agent name to delete")
    agent_reset_model = agent_sub.add_parser(
        "reset-model",
        help="Clear an agent spec's pinned model so it tracks the shipped default again",
    )
    agent_reset_model.add_argument(
        "--agent",
        # Literal rather than an import: this parser is built on every CLI
        # invocation and `agent.py` pulls in config.loader, which is the import
        # cost the lazy dispatch in main() exists to avoid. Same spelling the
        # resolver itself compares against in resolve_effective_model.
        default="kirocrew",
        help="Kiro agent spec to reset (default: kirocrew)",
    )


def register_workspace_parser(sub: _Commands) -> None:
    ws_parser = cli_help.add_command(sub, "workspace")
    ws_sub = ws_parser.add_subparsers(dest="workspace_action")
    ws_sub.add_parser("list", help="List workspaces")
    ws_create = ws_sub.add_parser("create", help="Create a workspace")
    ws_create.add_argument("--name", required=True, help="Workspace name")
    ws_create.add_argument(
        "--dir",
        default=None,
        help=(
            "Workspace directory NAME, relative to the KiroCrew data home "  # brand-ok: help text kept as it was
            "(default: workspace-<name>). Any path resolving outside the data "
            "home is rejected."
        ),
    )
    ws_create.add_argument("--copy-from", help="Copy dir from an existing workspace")
    ws_update = ws_sub.add_parser("update", help="Update a workspace")
    ws_update.add_argument("name", help="Workspace name to update")
    ws_update.add_argument(
        "--dir",
        help=(
            "New workspace directory NAME, relative to the KiroCrew data home. "  # brand-ok: help text kept as it was
            "Any path resolving outside the data home is rejected."
        ),
    )
    ws_delete = ws_sub.add_parser("delete", help="Delete a workspace")
    ws_delete.add_argument("name", help="Workspace name to delete")


def register_app_parser(sub: _Commands) -> None:
    app_parser = cli_help.add_command(
        sub,
        "app",
        epilog="""
Examples:
  kirocrew app install /path/to/oncall-watchtower
  kirocrew app list
  kirocrew app enable oncall-watchtower
  kirocrew app disable oncall-watchtower
  kirocrew app info oncall-watchtower
  kirocrew app uninstall oncall-watchtower
""",
        formatter_class=_fmt,
    )
    app_sub = app_parser.add_subparsers(dest="app_action")
    app_install = app_sub.add_parser("install", help="Install an app from a local directory")
    app_install.add_argument("source", help="Path to app directory containing app.json")
    app_import = app_sub.add_parser(
        "import",
        help="Convert a manifest-declared plugin package into an app directory",
    )
    app_import.add_argument("source", help="Path to the plugin package directory")
    app_import.add_argument(
        "--out",
        help="Output app directory (default: ./<app-name>-app)",
    )
    app_import.add_argument(
        "--name",
        help="Override the derived app name (kebab-case)",
    )
    app_import.add_argument(
        "--install",
        action="store_true",
        help="Install the converted app after writing it",
    )
    app_sub.add_parser("list", help="List installed apps")
    app_enable = app_sub.add_parser("enable", help="Enable an installed app")
    app_enable.add_argument("name", help="App name to enable")
    app_disable = app_sub.add_parser("disable", help="Disable an installed app")
    app_disable.add_argument("name", help="App name to disable")
    app_mcp = app_sub.add_parser(
        "mcp",
        help="Run an app's MCP server on stdio (spawned by kiro-cli, not for humans)",
    )
    app_mcp.add_argument("name", help="App name whose MCP server to run")
    app_uninstall = app_sub.add_parser(
        "uninstall", help="Uninstall an app (preserves app data by default)"
    )
    app_uninstall.add_argument("name", help="App name to uninstall")
    app_uninstall.add_argument(
        "--purge-data",
        action="store_true",
        help="Permanently delete the app data directory",
    )
    app_info = app_sub.add_parser("info", help="Show app details")
    app_info.add_argument("name", help="App name")
    app_dev = app_sub.add_parser(
        "dev",
        help="Toggle dev mode (no-store UI serving + live reload on file change)",
        # No flag abbreviations: --confirm-out-of-install-root is the
        # operator's out-of-install attestation and the builtin agent deny
        # rule matches its LITERAL text, so an accepted abbreviation
        # (`--confirm`, `--c`) would sail past the rule and let an agent
        # shell self-supply the attestation. Only the exact flag parses.
        allow_abbrev=False,
    )
    app_dev.add_argument("name", help="App name")
    app_dev.add_argument("--off", action="store_true", help="Turn dev mode off")
    app_dev.add_argument(
        "--confirm-out-of-install-root",
        action="store_true",
        help=(
            "Confirm enabling dev mode on a ui root that resolves outside the "
            "app's install directory (e.g. a ui/ symlinked to your source tree)"
        ),
    )
    app_init = app_sub.add_parser("init", help="Scaffold a new app")
    app_init.add_argument("name", help="App name (kebab-case)")
    app_init.add_argument("--dir", default=".", help="Output directory (default: current)")
    app_init.add_argument("--backend", action="store_true", help="Include backend stub")
    app_init.add_argument("--ui", action="store_true", help="Include UI frontend (ESM + Vite)")
    app_init.add_argument("--cron", action="store_true", help="Include sample cron job")
