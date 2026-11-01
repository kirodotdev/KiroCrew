"""The tool catalogue the cron MCP server returns for ``tools/list``.

One definition per cron tool: its name, description and input schema, with the
display titles :mod:`kiro_crew.mcp_tool_titles` adds.

Served through :mod:`kiro_crew.mcp_cron`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.cron_service.model import (
    _ALERT_AFTER_DEFAULT,
    _AUTO_PAUSE_MAX,
    _AUTO_PAUSE_THRESHOLD,
)
from kiro_crew.mcp_tool_titles import with_titles

# ``auto_pause_after_failures`` description for the ``cron_add`` and ``cron_update``
# tools. The cap and the default come from the cron model so the copy cannot drift.
_AUTO_PAUSE_AFTER_DESCRIPTION = (
    "Consecutive failed runs before this job auto-pauses "
    f"(0..{_AUTO_PAUSE_MAX}, default {_AUTO_PAUSE_THRESHOLD}; 0 = never auto-pause). "
    f"With 0 or a limit above {_AUTO_PAUSE_THRESHOLD}, retries from the "
    f"{_AUTO_PAUSE_THRESHOLD}th consecutive failure start 5 minutes apart and double "
    "after each further failure, up to one hour apart."
)
_ALERT_AFTER_DESCRIPTION = (
    "Send the failure message only after this many consecutive failed runs "
    f"(1..{_AUTO_PAUSE_MAX}, default {_ALERT_AFTER_DEFAULT}). The dashboard bell still "
    "records every failure; policy denials and the run that auto-pauses the job "
    "always alert."
)


def _list_tools() -> list[dict[str, Any]]:
    """Return MCP tool definitions."""
    tools: list[dict[str, Any]] = [
        {
            "name": "cron_list",
            "description": (
                "List scheduled cron jobs. By default returns a compact "
                "summary per job (id, name, status, schedule, next-run, "
                "kind, agent, channel, last-status, last-error/result "
                "preview, message preview) — sized to stay well under "
                "context budget even for large registries (50+ jobs). "
                "NOTE: the default response shape was compacted from the "
                "legacy verbose layout; programmatic callers that need "
                "byte-identical legacy output must pass verbose=true, or "
                'ids=["<job_id>", ...] to drill in on specific jobs. Set '
                "verbose=true for the full output (full message body and "
                'full last_error/last_result). Pass ids=["<job_id>", ...] '
                "to fetch full bodies for only those jobs (drill-in "
                "pattern after a compact list). ids takes precedence over "
                "verbose. SCOPED TO THE CALLING SESSION: only jobs this "
                "session owns are listed, so an empty result means none are "
                "owned HERE, not that none are scheduled. Jobs owned by "
                "another session, or created without one (the CLI, the "
                "onboarding importer), are managed with `kirocrew cron list` "
                "or on the dashboard Schedule page."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "verbose": {
                        "type": "boolean",
                        "description": "If true, return full per-job bodies "
                        "(legacy shape). Default false (compact summary).",
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of job IDs. When set, "
                        "returns full bodies for matching jobs only.",
                    },
                    "json": {
                        "type": "boolean",
                        "description": "If true, return a JSON document instead "
                        "of text: one record per owned job with its mode, "
                        "schedule, context settings, prompt, and folded "
                        "run-history counts (runs, failures, distinct results, "
                        "runs that found nothing to do). Takes precedence over "
                        "verbose and over ids' implied verbose, because it "
                        "serves a program rather than a reader. Run history is "
                        "read through the gateway, which is the only reader "
                        "that can see it; when that read cannot happen, "
                        "history_available is false and each UNFETCHED job's "
                        "history is null rather than an empty tally, so a job "
                        "whose history is unknown is never mistaken for an idle "
                        "one. A partial read keeps the history it did fetch.",
                    },
                },
            },
        },
        {
            "name": "cron_add",
            "description": (
                "Add a scheduled cron job. Use when the user says 'every', "
                "'daily', 'weekly', 'remind me', 'check regularly', or "
                "'schedule'. Requires name + message, plus one of: every "
                "(seconds), cron_expr, at (unix timestamp), delay (seconds "
                "from now), or at_time (human string like '5pm', "
                "'tomorrow 9am', 'in 2 hours')."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Job name"},
                    "message": {"type": "string", "description": "Message to send to agent"},
                    "every": {
                        "type": "integer",
                        "description": "Interval in seconds (min 60)",
                    },
                    "cron_expr": {
                        "type": "string",
                        "description": "Standard 5-field cron expression: "
                        '"min hour dom month dow" where dow: 0=Sun,1=Mon..6=Sat '
                        '(e.g. "0 9 * * 1-5" for weekdays at 9AM, '
                        '"30 15 * * 2,4" for Tue/Thu at 3:30PM), evaluated in '
                        "the job's timezone (default: the global config "
                        "timezone, then UTC)",
                    },
                    "at": {
                        "type": "number",
                        "description": "Unix timestamp for one-shot job (auto-deletes after)",
                    },
                    "delay": {
                        "type": "number",
                        "description": "Seconds from now for one-shot job (e.g. 120 for 2 minutes). "
                        "Converted to 'at' internally. Prefer this over 'at'.",
                    },
                    "at_time": {
                        "type": "string",
                        "description": "Human time string for one-shot job, parsed server-side. "
                        "Examples: '5pm', '17:00', 'tomorrow 9:30am', 'in 2 hours', "
                        "'in 3 days', '2026-03-28 14:00'. A clock time, and a relative "
                        "day or week count, is read in 'timezone' when given, "
                        "else the global config timezone, then UTC. "
                        "Prefer this over 'at' for absolute times.",
                    },
                    "channel": {
                        "type": "string",
                        "description": "Slack channel ID to post results to (e.g. 'C0AP3QR7Z4M'). "
                        "If omitted, posts in the originating thread/DM.",
                    },
                    "thread_ts": {
                        "type": "string",
                        "description": "Slack thread timestamp to reply in. "
                        "Use with channel to post results as a thread reply instead of a new message. "
                        "When omitted and the cron is scheduled from inside a Slack thread in the "
                        "same channel it posts to, it DEFAULTS to the caller's own thread. "
                        "Pass an empty string to opt out and deliver at the channel top level.",
                    },
                    "agent": {
                        "type": "string",
                        "description": "Agent name for this job (e.g. 'customer360-code-agent'). "
                        "Empty or omitted uses agent.default_agent, or the kirocrew "
                        "agent when that is unset or not eligible (e.g. a default "
                        "that restricts subagents).",
                    },
                    "member_id": {
                        "type": "string",
                        "description": "Crew Member responsible for this schedule. Uses that "
                        "member's memory. Omit to inherit the creating conversation's "
                        "member; ordinary conversations retain global V1 memory.",
                    },
                    "silent": {
                        "type": "boolean",
                        "description": "When true, suppress automatic message delivery. "
                        "The agent controls when to notify via send_message.",
                    },
                    "approval_mode": {
                        "type": "string",
                        "enum": ["", "auto"],
                        "description": "Tool approval mode for this job. "
                        "Empty or omitted uses default hook-based approval. "
                        "'auto' (auto-approve all tools) is refused here: only the "
                        "owner can turn it on, on the Schedule page.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model override for this job (canonical key or provider id, "
                        "e.g. 'sonnet', 'opus'). Empty or omitted inherits from the agent config "
                        "or global default. Applies when the job's session is created; a running "
                        "persistent session keeps its current model until it is reset.",
                    },
                    "skip_dates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": 'ISO dates to skip (e.g. ["2026-04-06", "2026-12-25"]). '
                        "Job silently does not fire on these dates. Evaluated in job's timezone.",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone for cron expression evaluation, an at_time "
                        "clock time, and skip_dates (e.g. 'America/New_York'). Cron hour/minute "
                        "fields and an at_time such as '9am' are interpreted in this timezone. "
                        "Falls back to global config timezone, then UTC.",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Schedule-page folder to file this job in, by name or "
                        "id (e.g. 'Veille'). A missing name is created. Empty or omitted "
                        "leaves the job ungrouped.",
                    },
                    "persistent_session": {
                        "type": "boolean",
                        "description": "Whether this cron reuses one agent session across "
                        "runs (True, default) or opens a fresh session per run (False). "
                        "Set False for polling/scanner jobs with no conversational state — "
                        "avoids unbounded context growth. Set True (or omit) for "
                        "conversational reminders that should remember prior runs.",
                    },
                    "minimal_context": {
                        "type": "boolean",
                        "description": "When true, skip memory, lessons, skills, and "
                        "thread history injection — only date/time and agent identity "
                        "are included (~200 tokens vs ~30-55k). Also caps last_result "
                        "to 2000 chars. Use for simple polling/checker jobs.",
                    },
                    "hide_in_chat": {
                        "type": "boolean",
                        "description": "When true, this cron's runs do NOT appear as a chat "
                        "session in the dashboard active-session list (default false). Set true "
                        "for fire-and-forget jobs (digests, cleanups, polling) so they stay out "
                        "of the Chats sidebar — the result still goes to Slack/dashboard "
                        "notification and the History tab. Only applies to agent crons "
                        "(LLM jobs with a message); script/command crons never create a slot.",
                    },
                    "strict_schedule": {
                        "type": "boolean",
                        "description": "When true, fire exactly on schedule with no jitter. "
                        "Default false — jobs get random delay (0-5min hourly, 0-59min daily) "
                        "to spread load.",
                    },
                    "script": {
                        "type": "string",
                        "description": "Python callable path for code-based cron execution "
                        "(bypasses LLM entirely). Format: "
                        "'~/.kiro/crew/crons/file.py:function'. Scripts must be under "
                        "~/.kiro/crew/crons/. Function receives a "
                        "ScriptContext and can raise Skip() to retry or Done() to "
                        "remove the job. Use ctx.notify() to deliver messages, and "
                        "ctx.open_session() / ctx.send_to_session() / "
                        "ctx.set_session_mode() to open, seed and set the approval "
                        "mode (trust or trust_reads) of a dashboard session (never "
                        "shell out to 'kirocrew token'; the sandboxed child is "
                        "refused). "
                        "When set, 'message' is passed to the script as ctx.message "
                        "(used for arguments) rather than being sent to an LLM.",
                    },
                    "command": {
                        "type": "string",
                        "description": "Shell command for code-based cron execution "
                        "(bypasses LLM entirely). Mutually exclusive with 'script'. "
                        "When set, 'message' is passed as arguments rather than "
                        "being sent to an LLM.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Execution timeout in seconds. "
                        "Defaults: 30s for scripts, 300s for commands. "
                        "Set higher for long-running tasks.",
                    },
                    "timeout_secs": {
                        "type": "integer",
                        "description": "Per-wake execution budget in seconds "
                        "(1..86400; the asyncio deadline for one run of this "
                        "job, default 1800). Distinct from 'timeout', which "
                        "bounds only script/command subprocesses. Raise it for "
                        "agents whose single wake legitimately outgrows 30 min.",
                    },
                    "auto_pause_after_failures": {
                        "type": "integer",
                        "description": _AUTO_PAUSE_AFTER_DESCRIPTION,
                    },
                    "alert_after_failures": {
                        "type": "integer",
                        "description": _ALERT_AFTER_DESCRIPTION,
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "cron_update",
            "description": "Update an existing cron job's name, message, schedule, agent, or channel.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Job ID to update"},
                    "name": {"type": "string", "description": "New job name"},
                    "message": {"type": "string", "description": "New message"},
                    "cron_expr": {"type": "string", "description": "New cron expression"},
                    "every": {"type": "integer", "description": "New interval in seconds (min 60)"},
                    "timeout": {
                        "type": "integer",
                        "description": "Script/command subprocess timeout in "
                        "seconds (0..3600; 0 = defaults: 30s script, 300s "
                        "command).",
                    },
                    "timeout_secs": {
                        "type": "integer",
                        "description": "Per-wake execution budget in seconds "
                        "(1..86400; the asyncio deadline for one run of this "
                        "job, default 1800). Distinct from 'timeout', which "
                        "bounds only script/command subprocesses. Raise it for "
                        "jobs whose single run legitimately outgrows 30 min.",
                    },
                    "auto_pause_after_failures": {
                        "type": "integer",
                        "description": _AUTO_PAUSE_AFTER_DESCRIPTION,
                    },
                    "alert_after_failures": {
                        "type": "integer",
                        "description": _ALERT_AFTER_DESCRIPTION,
                    },
                    "agent": {"type": "string", "description": "New agent name"},
                    "channel": {"type": "string", "description": "New channel ID"},
                    "folder": {
                        "type": "string",
                        "description": "Move the job to this Schedule-page folder, by name "
                        "or id. A missing name is created. Empty string moves the job out "
                        "of its folder (ungrouped).",
                    },
                    "thread_ts": {
                        "type": "string",
                        "description": "New thread timestamp to reply in.",
                    },
                    "approval_mode": {
                        "type": "string",
                        "enum": ["", "auto"],
                        "description": "New tool approval mode. '' turns auto-approve "
                        "off; 'auto' is refused here (owner-only, Schedule page).",
                    },
                    "silent": {"type": "boolean", "description": "Whether the job runs silently"},
                    "skip_dates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "ISO dates to skip. Replaces existing list. To empty "
                        "it, pass clear_skip_dates: true instead of an empty list.",
                    },
                    "clear_skip_dates": {
                        "type": "boolean",
                        "description": "true removes every skip date, so the job fires on "
                        "all its scheduled days again. Use it rather than skip_dates: [], "
                        "which some clients drop before it reaches the server. Refused "
                        "beside a non-empty skip_dates.",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone for cron expression evaluation and "
                        "skip_dates. Falls back to global config timezone, then UTC.",
                    },
                    "strict_schedule": {
                        "type": "boolean",
                        "description": "When true, fire exactly on schedule with no jitter.",
                    },
                    "persistent_session": {
                        "type": "boolean",
                        "description": "Whether this cron reuses one agent session across runs.",
                    },
                    "minimal_context": {
                        "type": "boolean",
                        "description": "When true, skip memory/lessons/skills/history "
                        "injection. Only date/time + agent identity are included.",
                    },
                    "hide_in_chat": {
                        "type": "boolean",
                        "description": "When true, this cron's runs do NOT appear as a chat "
                        "session in the dashboard active-session list. Set true to keep "
                        "fire-and-forget jobs out of the Chats sidebar (result still goes to "
                        "Slack/bell + History).",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model override for this job (canonical key or provider id). "
                        "Empty string clears the override (inherits from agent/global). Applies "
                        "when the job's session is created; a running persistent session keeps "
                        "its current model until it is reset.",
                    },
                },
                "required": ["job_id"],
            },
        },
        {
            "name": "cron_remove",
            "description": "Remove a cron job by ID",
            "inputSchema": {
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "Job ID"}},
                "required": ["job_id"],
            },
        },
        {
            "name": "cron_remove_all",
            "description": "Remove all cron jobs owned by this session (other "
            "sessions' jobs are untouched; use `kirocrew cron` for those)",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "cron_pause",
            "description": "Pause a cron job",
            "inputSchema": {
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "Job ID"}},
                "required": ["job_id"],
            },
        },
        {
            "name": "cron_resume",
            "description": "Resume a paused cron job",
            "inputSchema": {
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "Job ID"}},
                "required": ["job_id"],
            },
        },
        {
            "name": "cron_trigger",
            "description": "Trigger immediate execution of a cron job regardless of its schedule",
            "inputSchema": {
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "Job ID to trigger"}},
                "required": ["job_id"],
            },
        },
        {
            "name": "cron_secret_request",
            "description": (
                "Request vault secrets for a SCRIPT cron job you own. "
                "This does NOT grant anything: it records a PENDING request "
                "(env-var name -> vault secret name, pinned to the job's "
                "current code) that the operator must approve in the dashboard "
                "(Schedule > job > Secrets) before the values are injected "
                "into the job's subprocess env at fire time. Tell the user to "
                "approve it. Secrets must already exist in the vault "
                "(Settings > Secrets). To withdraw a pending request, pass "
                "withdraw: true with no 'secrets'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Job ID to request secrets for"},
                    "secrets": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "Mapping of env-var name (e.g. 'MY_SANDBOX_TOKEN', "
                            "[A-Z][A-Z0-9_]*) to the vault secret name to "
                            "inject under it. Required unless withdraw is true. "
                            "An empty object also withdraws the pending request, "
                            "but some clients drop it before it reaches the "
                            "server, so use withdraw for that."
                        ),
                    },
                    "withdraw": {
                        "type": "boolean",
                        "description": (
                            "true withdraws this job's pending secret request. "
                            "Pass it without 'secrets'; it is refused beside a "
                            "non-empty mapping."
                        ),
                    },
                },
                "required": ["job_id"],
            },
        },
    ]
    return with_titles("kirocrew-cron", tools)
