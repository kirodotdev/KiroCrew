"""MCP server exposing cron tools to kiro-cli.

Runs as ``kirocrew mcp-cron`` — kiro-cli spawns it as a child process
and calls tools via JSON-RPC over stdio (MCP protocol).

Tools:
    cron_list       — list all scheduled jobs
    cron_add        — add a cron job (every/cron/at)
    cron_remove     — remove a job by ID
    cron_remove_all — remove all jobs
    cron_pause      — pause a job
    cron_resume     — resume a paused job
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import available_timezones

from kiro_crew import model_registry
from kiro_crew.config.loader import DASHBOARD_PORT, config_dir
from kiro_crew.cron import CronService, compute_next_run_ts, format_schedule, get_local_tz
from kiro_crew.cron_script import resolve_script_path
from kiro_crew.cron_trigger import trigger_cron_job
from kiro_crew.mcp_core import _resolve_session_key
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.platform import current_context
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.sandbox import _AGENT_DENIED_ENV_KEYS
from kiro_crew.security import (
    _SENSITIVE_HOME_DIRS,
    audit_bash_exfiltration,
    is_sensitive_bash_command,
    is_sensitive_path,
    scan_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.validation import MCP_CRON_SCHEMAS, ValidationError, validate_tool_args

logger = logging.getLogger(__name__)

# Patterns for _parse_time_string
_RE_IN_DURATION = re.compile(
    r"^in\s+(\d+)\s*(s|sec|second|seconds|m|min|minute|minutes|h|hr|hour|hours)$", re.I
)
_UNIT_SECS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "hours": 3600,
}


# Credential dirs/files a cron shell command must never reference directly. The
# sandbox (cron_script.run_command_sandboxed, mode="cc") is the only
# sanctioned access path. We reuse security._SENSITIVE_HOME_DIRS (the canonical
# list, kept DRY so it can't drift) and match the token ANYWHERE in the command
# — not only after a known read command like the shared is_sensitive_bash_command
# regex does — because tools such as ``curl -d @~/.aws/credentials`` or
# ``wget --post-file=$HOME/.ssh/id_rsa`` read files via flags with no recognizable
# read-command prefix, evading that regex (verified: the canonical exfil payload
# slipped through the three stock guards).
_CRON_CRED_PATH_RE = re.compile(
    r"(?:^|[\s'\"=@/~`]|\$\{?HOME\}?)"
    r"(?:" + "|".join(re.escape(d) for d in _SENSITIVE_HOME_DIRS) + r")"
    r"(?:/|\s|['\"]|$)",
    re.IGNORECASE,
)
# Protected secret env vars a cron command must not read by name. Union of the
# sandbox-scrubbed agent keys (Slack tokens, owner id) and well-known cloud /
# source-control credential env vars. Fix 4 strips _AGENT_DENIED_ENV_KEYS from
# the cron subprocess env, but AWS_*/token vars may still be present (or arrive
# via a future regression), so denying a by-name reference at storage time is a
# cheap, precise backstop that mirrors the existing execute_bash deniedCommands
# AWS patterns in config/defaults.json.
_CRON_SECRET_ENV_NAMES = list(_AGENT_DENIED_ENV_KEYS) + [
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITLAB_TOKEN",
]
_CRON_SECRET_ENV_RE = re.compile(
    r"\$\{?(?:" + "|".join(re.escape(k) for k in _CRON_SECRET_ENV_NAMES) + r")\}?",
    re.IGNORECASE,
)
# Bare-name form for scanning SCRIPT bodies: a Python/Ruby cron script reads
# secrets by env-var NAME (e.g. os.environ["AWS_SECRET_ACCESS_KEY"]), not via
# the shell ``$NAME`` syntax, so we also match the names on word boundaries.
_CRON_SECRET_NAME_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in _CRON_SECRET_ENV_NAMES) + r")\b",
)
# Cap how much of a cron script we read for the security review (256 KiB is far
# larger than any legitimate cron script; bounds memory on a hostile huge file).
_MAX_SCRIPT_SCAN_BYTES = 256 * 1024


def _audit_governance_deny(session_key: str, tool_name: str, scope: str, decision: object) -> None:
    """Best-effort SEL audit of an out-of-band governance denial (file-backed).

    Mirrors hooks._audit_governance so the chokepoint denials beyond the host
    gate (cron capability, etc.) leave the same ``governance_decision`` forensic
    trail. Never raises (audit must not wedge the deny path).
    """
    try:
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=session_key,
            tool_name=tool_name,
            scope=scope,
            outcome="denied",
            rule=getattr(decision, "rule", ""),
            layer=getattr(decision, "layer", ""),
            reason=getattr(decision, "reason", ""),
        )
    except Exception:
        logger.debug("governance deny audit emit failed", exc_info=True)


def _vet_cron_capability_governance() -> str | None:
    """Apply the ``capabilities.cron`` gate before authoring ANY cron job.

    Distinct from :func:`_vet_command_governance` (which gates the command
    *body* under the ``commands`` scope): this is the on/off *capability* gate.
    When a policy/profile sets ``capabilities.cron.enabled = false`` for the
    calling surface, no cron job may be authored at all — regardless of whether
    it carries a command, a script, or only a message.  ``capabilities.cron``
    defaults OFF in the catalog, so a profile that does not mention it leaves
    cron bounded by policy alone (profile-absence = not-governed, the documented
    deviation) — only an explicit ``enabled: false`` (or a deny-all profile)
    disables it.  Best-effort beyond the caller's always-on guards.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    # Resolve the session key BEFORE the try so it is bound in the except branch.
    # Fall back to a ``cron:``-prefixed key so an empty session key still
    # classifies to the CRON surface (a bare "mcp_cron" misclassifies to the
    # attended "slack" surface via sel._infer_source, skipping a cron-bound
    # profile) — matching _vet_command_governance's "cron:_vet".
    sk = _resolve_session_key() or "cron:_vet"
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        # item="" → the CapabilityGate's ``enabled`` flag is what is queried.
        # log_warning=False: this runs inside the kirocrew-cron stdio MCP server,
        # whose stray stderr would corrupt the JSON-RPC stream — the degrade
        # WARNING is suppressed (the file-backed SEL is still written).  The inner
        # call carries the flag too because governance_permits catches the common
        # resolution error itself and never re-raises to the outer except below.
        decision = governance_permits("capabilities.cron", "", session_key=sk, log_warning=False)
        if not getattr(decision, "permitted", True):
            _audit_governance_deny(sk, "cron_add", "capabilities.cron", decision)
            return "Error: cron scheduling blocked by governance policy: " + redact(
                getattr(decision, "reason", "cron capability disabled")
            )
    except PlatformCompositionError:
        raise
    except Exception:
        # Wrapped: a late-import failure must not raise out of this except-branch
        # and hard-fail the stdio kirocrew-cron tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "cron_add", session_key=sk, scope="capabilities.cron", log_warning=False
            )
        except Exception:
            pass
    return None


def _vet_command_governance(command: str) -> str | None:
    """Apply the governance ``commands`` ceiling ∩ cron profile to a cron command.

    The cron command executes via ``sh -c`` outside the ACP hook flow, so the
    host gate's governance check (hooks.on_tool_call) never runs on it.  We
    evaluate it here against the ``cron`` surface so an enterprise command deny
    or a per-cron profile's command scope still applies.  Best-effort: any
    governance error returns None (the always-on guards in the caller stand).
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        # log_warning=False: stdio kirocrew-cron server (see _vet_cron_capability_
        # governance) — suppress the degrade WARNING, keep the file-backed SEL.
        decision = governance_permits(
            "commands", command, session_key="cron:_vet", log_warning=False
        )
        if not getattr(decision, "permitted", True):
            return "Error: cron command blocked by governance policy: " + redact(
                getattr(decision, "reason", "")
            )
    except PlatformCompositionError:
        # Fail-closed CPP invariant: a host that could not compose its companion
        # must abort, never silently fall open. Always propagate.
        raise
    except Exception:
        # Wrapped: a late-import failure must not hard-fail the stdio cron tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "cron_command", session_key="cron:_vet", scope="commands", log_warning=False
            )
        except Exception:
            pass
    return None


def _vet_shell_command(command: str) -> str | None:
    """Apply the bash-tool security guards to a model-supplied cron shell command.

    The ``command`` field of ``cron_add`` is a free-form shell string that is
    later executed by the gateway via ``sh -c`` (see ``cron_script.run_command_sandboxed``),
    entirely outside the kiro-cli ACP permission/hook flow. The host hook layer
    only ever sees the tool name ``"cron_add"``, never the embedded command, so
    the deny-list/sensitive-path checks that normally gate a ``bash`` tool call
    never run on this path. We therefore replicate them here, at storage time,
    so a prompt-injected ``cron_add`` cannot schedule credential exfiltration or
    arbitrary destructive shell. Mirrors the same guards used for the bash tool
    in ``security.py`` (``is_denied`` / ``is_sensitive_bash_command`` /
    ``scan_exfiltration_urls``), plus a cron-surface-specific deny of any
    credential-path or protected-secret-env reference (the stock guards miss
    flag-based file reads like ``curl -d @FILE`` and body-exfil, which is the
    exact gap this closes).

    Returns an ``"Error: ..."`` string to surface to the caller, or ``None`` if
    the command is clean. The returned message is redacted so it never echoes
    captured credentials back to the model.
    """
    if not command:
        return None
    # Route the deny check through the active PlatformContext's PolicyAuthority so
    # the companion's ADD-only deny overlay applies to cron commands too (the same
    # enforcement hooks.on_tool_call uses). The Default authority evaluates
    # BASELINE_DENY only, so standalone is byte-for-byte unchanged. This is the
    # ONLY deny gate for the cron `command` field — it executes via ``sh -c``
    # outside the kiro-cli ACP permission/hook flow — so an overlay-only deny
    # pattern would otherwise be silently bypassed here.
    reason = (
        current_context().security.is_denied(command)
        or is_sensitive_bash_command(command)
        or audit_bash_exfiltration(command)
    )
    if reason:
        # Scrub the echoed reason through the SAME context the deny check used,
        # so a companion-overlay-detected token does not leak in the message
        # returned to the model.
        safe_reason = redact(reason)
        return f"Error: cron command blocked by security policy: {safe_reason}"
    # Governance: the cron `command` runs out-of-band (sh -c), so the host gate
    # never sees it — apply the governance ceiling ∩ cron profile here against
    # the cron surface. Covers both an enterprise commands-deny and the per-cron
    # profile's command scope. Best-effort beyond the always-on checks above.
    gov_reason = _vet_command_governance(command)
    if gov_reason:
        return gov_reason
    if _CRON_CRED_PATH_RE.search(command):
        return (
            "Error: cron command blocked: references a credential path "
            "(e.g. .aws/.ssh/.netrc). Cron commands may not read credential "
            "files directly."
        )
    if _CRON_SECRET_ENV_RE.search(command):
        return "Error: cron command blocked: references a protected secret environment variable"
    exfil = scan_exfiltration_urls(command)
    if exfil:
        safe = redact("; ".join(exfil))
        return f"Error: cron command blocked: possible credential exfiltration ({safe})"
    return None


def _vet_script_contents(text: str) -> str | None:
    """Scan a cron SCRIPT body for credential-exfiltration patterns.

    A ``cron_add`` ``script`` job points at a file under ``~/.kirocrew/crons/``
    that the agent itself can write (via its file-write tool) and then register.
    ``resolve_script_path`` validates only the *path*, so without this the body
    is never inspected. The script runs under ``mode="standard"`` (user scripts
    may legitimately use creds), which does NOT hide ``~/.aws`` — so a body that
    reads ``~/.aws/credentials`` or ``os.environ["AWS_SECRET_ACCESS_KEY"]`` and
    POSTs it out would succeed. We reuse the same credential-path / secret-env /
    exfil detectors as the command path (plus a bare-name env match for
    ``os.environ[...]`` style access). We deliberately do NOT run ``is_denied``
    over a script body: it encodes shell tool-name semantics (e.g. ``*git*push*``)
    that false-positive on ordinary Python source, and destructive-op risk is
    covered by the now-required ``cron_add`` approval prompt (Fix 3). Credential
    exfiltration — which a human rubber-stamping the prompt would not catch — is
    the threat this gate closes (defense-in-depth item 5).
    """
    if _CRON_CRED_PATH_RE.search(text):
        return (
            "Error: cron script blocked: references a credential path "
            "(e.g. .aws/.ssh/.netrc). Cron scripts may not read credential files."
        )
    if _CRON_SECRET_ENV_RE.search(text) or _CRON_SECRET_NAME_RE.search(text):
        return "Error: cron script blocked: references a protected secret environment variable"
    reason = is_sensitive_bash_command(text)
    if reason:
        safe_reason = redact(reason)
        return f"Error: cron script blocked by security policy: {safe_reason}"
    exfil = scan_exfiltration_urls(text)
    if exfil:
        safe = redact("; ".join(exfil))
        return f"Error: cron script blocked: possible credential exfiltration ({safe})"
    return None


def _vet_script_file(file_path: str) -> str | None:
    """Read a resolved cron script file and run :func:`_vet_script_contents`.

    ``file_path`` is expected to come from ``resolve_script_path`` (under
    ``~/.kirocrew/crons/``), but this function does NOT trust that — it
    independently resolves the real path and rejects it via ``is_sensitive_path``
    before opening, so a symlink under the crons dir pointing at a credential
    file (e.g. ``crons/evil.py -> ~/.aws/credentials``) cannot be read here. Read
    is capped at ``_MAX_SCRIPT_SCAN_BYTES``. Storage-time check only (TOCTOU note:
    the file could change before execution — the exec-time sandbox is the runtime
    control; this gate stops the obvious register-a-malicious-script case).
    """
    try:
        resolved = Path(file_path).resolve()
    except (OSError, ValueError) as e:
        return f"Error: cannot resolve cron script path for security review: {e}"
    if is_sensitive_path(str(resolved)):
        return "Error: cron script path blocked by security policy (resolves to a sensitive credential path)"
    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            contents = f.read(_MAX_SCRIPT_SCAN_BYTES)
    except OSError as e:
        return f"Error: cannot read cron script for security review: {e}"
    return _vet_script_contents(contents)


def _log_cron_denial(tool_name: str, error: str) -> None:
    """Emit a SEL audit event when a cron command/script is blocked at storage time.

    The blocked command/script never reaches the kiro-cli ACP permission/hook
    flow (which normally produces the tool-invocation audit trail), so the denial
    must be recorded here to preserve the audit trail.
    ``error`` is the already-redacted "Error: ..." message from the _vet_* guards.
    """
    try:
        sel().log_tool_invocation(
            session_key=_resolve_session_key() or "mcp_cron",
            source="mcp",
            tool_name=tool_name,
            tool_kind="authz",
            outcome="denied",
            error=error,
        )
    except Exception:
        logger.debug("SEL logging failed for cron denial", exc_info=True)


def _parse_time_string(s: str) -> float | str:
    """Parse a human time string into a Unix timestamp. Returns error string on failure."""
    s = s.strip()
    _, tz = get_local_tz()
    now = datetime.now(tz)

    # "in 5 minutes", "in 2 hours"
    m = _RE_IN_DURATION.match(s)
    if m:
        secs = int(m.group(1)) * _UNIT_SECS[m.group(2).lower()]
        return time.time() + secs

    # Try common formats with optional "tomorrow"
    tomorrow = False
    text = s
    if text.lower().startswith("tomorrow"):
        tomorrow = True
        text = re.sub(r"^at\b\s*", "", text[8:].strip())

    # "5pm", "5:30pm", "17:00", "9:30am"
    for fmt in ("%I%p", "%I:%M%p", "%H:%M", "%I %p", "%I:%M %p"):
        try:
            parsed = datetime.strptime(text, fmt)
            result = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
            if tomorrow:
                result += timedelta(days=1)
            elif result <= now:
                result += timedelta(days=1)  # "5pm" when it's already 6pm → tomorrow
            return result.timestamp()
        except ValueError:
            continue

    # ISO-ish: "2026-03-28 14:00", "2026-03-28T14:00"
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=now.tzinfo)
            return parsed.timestamp()
        except ValueError:
            continue

    return f"Error: could not parse time '{s}'. Examples: '5pm', 'in 30 minutes', 'tomorrow 9am'"


def _list_tools() -> list[dict[str, Any]]:
    """Return MCP tool definitions."""
    return [
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
                "ids=[\"<job_id>\", ...] to drill in on specific jobs. Set "
                "verbose=true for the full output (full message body and "
                "full last_error/last_result). Pass ids=[\"<job_id>\", ...] "
                "to fetch full bodies for only those jobs (drill-in "
                "pattern after a compact list). ids takes precedence over "
                "verbose."
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
                        '(e.g. "0 9 * * 1-5" for weekdays at 9AM UTC, '
                        '"30 15 * * 2,4" for Tue/Thu at 3:30PM UTC)',
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
                        "'2026-03-28 14:00'. Uses server local timezone. "
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
                        "Use with channel to post results as a thread reply instead of a new message.",
                    },
                    "agent": {
                        "type": "string",
                        "description": "Agent name for this job (e.g. 'customer360-code-agent'). "
                        "Empty or omitted uses the default kirocrew agent.",
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
                        "'auto' auto-approves all tools without prompting. "
                        "Empty or omitted uses default hook-based approval.",
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
                        "description": "ISO dates to skip (e.g. [\"2026-04-06\", \"2026-12-25\"]). "
                        "Job silently does not fire on these dates. Evaluated in job's timezone.",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone for cron expression evaluation and "
                        "skip_dates (e.g. 'America/New_York'). Cron hour/minute fields are "
                        "interpreted in this timezone. Falls back to global config timezone, "
                        "then UTC.",
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
                        "'~/.kirocrew/crons/file.py:function'. Scripts must be under "
                        "~/.kirocrew/crons/. Function receives a "
                        "ScriptContext and can raise Skip() to retry or Done() to "
                        "remove the job. Use ctx.notify() to deliver messages. "
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
                    "agent": {"type": "string", "description": "New agent name"},
                    "channel": {"type": "string", "description": "New channel ID"},
                    "thread_ts": {
                        "type": "string",
                        "description": "New thread timestamp to reply in.",
                    },
                    "approval_mode": {
                        "type": "string",
                        "enum": ["", "auto"],
                        "description": "New tool approval mode",
                    },
                    "silent": {"type": "boolean", "description": "Whether the job runs silently"},
                    "skip_dates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "ISO dates to skip. Replaces existing list.",
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
            "description": "Remove all cron jobs",
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
    ]


# ── cron_list rendering ──

# Compact-mode caps. Tuned so 50 jobs stay well under 30KB.
_MSG_PREVIEW_LEN = 80
_ERR_PREVIEW_LEN = 200
_RESULT_PREVIEW_LEN = 120


def _format_next_run(job: Any, now: float, local_tz: Any) -> str:
    """Format the 'Next run' suffix for a job, or empty string if no next run."""
    nxt = compute_next_run_ts(job, now=now)
    if nxt is None:
        return ""
    delta = nxt - now
    if delta >= 86400:
        d = int(delta // 86400)
        h = int((delta % 86400) // 3600)
        rel = f"in {d}d {h}h"
    elif delta >= 3600:
        h = int(delta // 3600)
        m = int((delta % 3600) // 60)
        rel = f"in {h}h {m}m"
    elif delta > 0:
        m = int(delta // 60)
        rel = f"in {m}m" if m >= 1 else "in <1m"
    else:
        rel = "now"
    local_str = datetime.fromtimestamp(nxt, tz=local_tz).strftime("%Y-%m-%d %I:%M %p %Z")
    return f"\n  Next run: {local_str} ({rel})"


def _sanitize(s: str) -> str:
    """Redact credentials and exfiltration URLs from a string.

    Routes through the context-aware ``redact`` shim so a loaded companion's
    extra regexes apply; standalone is byte-for-byte today's two-pass.
    """
    return redact(s)


def _job_kind(job: Any) -> str:
    """Return 'script' / 'command' / 'agent' for a cron job."""
    if getattr(job, "script", ""):
        return "script"
    if getattr(job, "command", ""):
        return "command"
    return "agent"


def _render_cron_list_full(jobs: list[Any]) -> str:
    """Legacy (verbose) cron_list output — full message body per job.

    This rendering MUST stay byte-for-byte identical to the pre-change
    output so that ``verbose=true`` is regression-safe for existing
    callers that parse this format.
    """
    active = sum(1 for j in jobs if j.enabled)
    paused = len(jobs) - active
    header = f"{len(jobs)} cron job(s): {active} active, {paused} paused\n"
    lines: list[str] = [header]
    now = time.time()
    tz_name, local_tz = get_local_tz()
    for j in jobs:
        status = "✅ active" if j.enabled else "⏸️ paused"
        sched = format_schedule(j.schedule, tz_name=j.timezone or tz_name)
        next_line = _sanitize(_format_next_run(j, now, local_tz))
        san_name = _sanitize(j.name)
        san_msg = _sanitize(j.message)
        san_sched = _sanitize(sched)
        kind = _job_kind(j)
        entry = f"• {san_name} ({status}) [{kind}]\n  ID: {j.id} | {san_sched}{next_line}\n  → {san_msg}"
        if j.last_status == "error" and j.last_error:
            san_err = _sanitize(j.last_error)
            entry += f"\n  ⚠️ last error: {san_err}"
        elif (
            j.last_status == "ok"
            and (j.script or j.command)
            and j.last_result
            and j.last_result != "ok"
        ):
            san_res = _sanitize(j.last_result)[:_RESULT_PREVIEW_LEN]
            entry += f"\n  last result: {san_res}"
        lines.append(entry)
    return "\n".join(lines)


def _render_cron_list_compact(jobs: list[Any]) -> str:
    """Compact cron_list output — one short summary block per job.

    Drops full message bodies in favour of an 80-char preview, and
    truncates last_error / last_result to bounded sizes. Adds kind /
    agent / channel / last-status signal that the legacy format omitted
    or only included on certain branches. Sized so a 50-job registry
    stays well under 30KB.

    Sanitize-then-truncate ordering is enforced for every
    user-controlled field so a credential straddling the truncation
    boundary cannot leak as a partial fragment.

    Callers that need full bodies pass ``verbose=true`` (legacy shape)
    or ``ids=["<job_id>", ...]`` (drill-in for specific jobs).
    """
    active = sum(1 for j in jobs if j.enabled)
    paused = len(jobs) - active
    # Header intentionally bare — programmatic parsers lock on the regex
    # ``^\d+ cron job\(s\): \d+ active, \d+ paused$``. The "compact mode"
    # hint and the ``verbose=true`` / ``ids=[...]`` opt-outs are documented
    # on the cron_list tool description instead, where MCP clients see them.
    header = f"{len(jobs)} cron job(s): {active} active, {paused} paused\n"
    lines: list[str] = [header]
    now = time.time()
    tz_name, local_tz = get_local_tz()
    for j in jobs:
        status = "✅" if j.enabled else "⏸️"
        sched = format_schedule(j.schedule, tz_name=j.timezone or tz_name)
        next_line = _sanitize(_format_next_run(j, now, local_tz))
        san_name = _sanitize(j.name)
        san_sched = _sanitize(sched)
        kind = _job_kind(j)
        # Sanitize-then-truncate: truncating first could split a credential
        # or exfiltration URL across the boundary and bypass redaction.
        # Newlines also collapsed so each job stays a single block.
        msg_raw = j.message if isinstance(j.message, str) else ""
        msg = msg_raw.strip().replace("\n", " ")
        san_msg_full = _sanitize(msg) if msg else ""
        if len(san_msg_full) > _MSG_PREVIEW_LEN:
            san_msg_preview = san_msg_full[:_MSG_PREVIEW_LEN] + "…"
        else:
            san_msg_preview = san_msg_full
        # Optional signal lines — only emit when present, to keep small jobs short.
        extras: list[str] = []
        agent_raw = getattr(j, "agent_id", "")
        agent = agent_raw.strip() if isinstance(agent_raw, str) else ""
        channel_raw = getattr(j, "channel", None)
        channel = channel_raw.strip() if isinstance(channel_raw, str) else ""
        if agent:
            extras.append(f"agent={_sanitize(agent)}")
        model_raw = getattr(j, "model", "")
        model_val = model_raw.strip() if isinstance(model_raw, str) else ""
        if model_val:
            # _sanitize applies the full redact_credentials +
            # redact_exfiltration_urls chain required for LLM-controlled values.
            extras.append(f"model={_sanitize(model_val)}")
        if channel:
            extras.append(f"channel={_sanitize(channel)}")
        last_status = getattr(j, "last_status", None)
        if isinstance(last_status, str) and last_status:
            extras.append(f"last={_sanitize(last_status)}")
        # Show last_error preview when in error; otherwise last_result for
        # script/command jobs whose result isn't the trivial "ok".
        if last_status == "error" and isinstance(j.last_error, str) and j.last_error:
            san_err = _sanitize(j.last_error)
            err_short = (
                san_err
                if len(san_err) <= _ERR_PREVIEW_LEN
                else san_err[:_ERR_PREVIEW_LEN] + "…"
            )
            extras.append(f"err={err_short}")
        elif (
            last_status == "ok"
            and (getattr(j, "script", "") or getattr(j, "command", ""))
            and isinstance(j.last_result, str)
            and j.last_result
            and j.last_result != "ok"
        ):
            san_res = _sanitize(j.last_result)
            res_short = (
                san_res
                if len(san_res) <= _RESULT_PREVIEW_LEN
                else san_res[:_RESULT_PREVIEW_LEN] + "…"
            )
            extras.append(f"result={res_short}")
        extras_line = f"\n  {' | '.join(extras)}" if extras else ""
        msg_line = f"\n  → {san_msg_preview}" if san_msg_preview else ""
        lines.append(
            f"• {san_name} {status} [{kind}]\n  ID: {j.id} | {san_sched}"
            f"{next_line}{extras_line}{msg_line}"
        )
    return "\n".join(lines)


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against schema. Returns cleaned args."""
    schema = MCP_CRON_SCHEMAS.get(name)
    if schema:
        cleaned = validate_tool_args(args, schema)
    else:
        cleaned = args  # tools without schemas (cron_remove_all) pass through
    # Semantic check: reject past timestamps for one-shot jobs
    at_ts = cleaned.get("at")
    if at_ts is not None and at_ts < time.time():
        raise ValidationError("at", f"timestamp {int(at_ts)} is in the past")
    return cleaned


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    """Execute a cron tool and return the result as text."""
    return call_tool_with_logging(
        name, raw_args, _validate_args, _call_tool_inner,
        session_key="mcp_cron", downstream_service="kirocrew-cron",
    )


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    """Execute a cron tool (post-validation)."""
    svc = CronService(base_dir=config_dir())

    if name == "cron_list":
        verbose = bool(args.get("verbose", False))
        ids_filter = args.get("ids") or None
        jobs = svc.list_jobs(include_disabled=True)
        if not jobs:
            return "No cron jobs."
        # Drill-in: ids filter forces full bodies for matching jobs only.
        if ids_filter:
            id_set = set(ids_filter)
            jobs = [j for j in jobs if j.id in id_set]
            if not jobs:
                missing = ", ".join(sorted(id_set))
                return f"No cron jobs match ids: {missing}"
            verbose = True
        if verbose:
            return _render_cron_list_full(jobs)
        return _render_cron_list_compact(jobs)

    if name == "cron_add":
        # Capability gate FIRST: if the calling surface's policy/profile disables
        # the cron capability, no job may be authored at all (command, script, or
        # message). This is the on/off gate, distinct from the per-command body
        # check below (the ``commands`` scope).
        cap_err = _vet_cron_capability_governance()
        if cap_err:
            _log_cron_denial("cron_add", cap_err)
            return cap_err
        n = args["name"]
        msg = args.get("message", "")
        script = args.get("script", "")
        command = args.get("command", "")
        if command:
            err = _vet_shell_command(command)
            if err:
                _log_cron_denial("cron_add", err)
                return err
        if script:
            try:
                script_path, _ = resolve_script_path(script)
            except (ValueError, FileNotFoundError, PermissionError) as e:
                return f"Error: {e}"
            err = _vet_script_file(script_path)
            if err:
                _log_cron_denial("cron_add", err)
                return err
        every = args.get("every")
        cron_expr = args.get("cron_expr")
        at_ts = args.get("at")
        delay = args.get("delay")
        at_time = args.get("at_time")
        if delay is not None and at_ts is None:
            at_ts = time.time() + delay
        if at_time is not None and at_ts is None:
            parsed = _parse_time_string(at_time)
            if isinstance(parsed, str):
                return parsed  # error message
            at_ts = parsed
        # Guard against past timestamps from any source (at, delay, at_time)
        if at_ts is not None and at_ts < time.time():
            local = datetime.fromtimestamp(at_ts).astimezone()
            return f"Error: resolved time {local.strftime('%I:%M %p %Z')} is in the past"
        channel = (args.get("channel") or "").strip() or None
        if channel is None:
            channel = os.environ.get("KIROCREW_CHANNEL_ID") or None
        if not every and not cron_expr and not at_ts:
            return "Error: provide every, cron_expr, at, delay, or at_time"
        # Validate model BEFORE add_job so an invalid value never leaves an
        # orphaned job behind (a retried cron_add would then duplicate it).
        model_arg = str(args.get("model") or "").strip()
        if model_arg:
            # No membership gate: the model list is sourced from the live
            # kiro-cli `--list-models` (via /api/models), not the claude_code
            # registry family, so any id the CLI advertises is valid. Matches
            # the chat model path (which also skips membership); the runtime is
            # model-agnostic with a gateway fallback. Only normalize the "auto"
            # inherit sentinel.
            resolved_model = model_registry.to_provider_id(model_arg, "claude_code")
            if resolved_model == "":
                # "auto" sentinel (canonical key with no pinned provider id):
                # explicit inherit — same as leaving model unset.
                model_arg = ""
        try:
            thread_ts = (args.get("thread_ts") or "").strip() or None
            job = svc.add_job(
                name=n,
                message=msg,
                every_secs=every,
                cron_expr=cron_expr,
                at_ts=at_ts,
                channel=channel,
                thread_ts=thread_ts,
                delete_after_run=bool(at_ts),
            )
        except ValueError as e:
            return f"Error: {e}"
        agent = args.get("agent", "")
        silent = args.get("silent", False)
        approval_mode = args.get("approval_mode", "")
        session_key = _resolve_session_key()
        if agent:
            job.agent_id = agent
        if silent:
            job.silent = True
        if approval_mode:
            job.approval_mode = approval_mode
        if model_arg:
            job.model = model_arg
        if session_key:
            job.session_key = session_key
        skip_dates = args.get("skip_dates", [])
        tz = args.get("timezone", "")
        if skip_dates:
            job.skip_dates = skip_dates
        if tz:
            if tz not in available_timezones():
                safe_tz = redact(tz)
                return f"Error: invalid timezone: {safe_tz!r}"
            job.timezone = tz
        # persistent_session: only override the default (True) when explicitly provided.
        # Type is enforced by validation.py CRON_ADD_SCHEMA (FieldSpec ... bool),
        # so we do NOT accept raw-truthy values here — only a real bool.
        persistent_session = args.get("persistent_session")
        persistent_session_explicit = isinstance(persistent_session, bool)
        if isinstance(persistent_session, bool):
            job.persistent_session = persistent_session
        minimal_context = args.get("minimal_context")
        minimal_context_explicit = isinstance(minimal_context, bool)
        if isinstance(minimal_context, bool):
            job.minimal_context = minimal_context
        hide_in_chat = args.get("hide_in_chat")
        hide_in_chat_explicit = isinstance(hide_in_chat, bool)
        if isinstance(hide_in_chat, bool):
            job.hide_in_chat = hide_in_chat
        strict_schedule = args.get("strict_schedule")
        strict_schedule_explicit = isinstance(strict_schedule, bool)
        if isinstance(strict_schedule, bool):
            job.strict_schedule = strict_schedule
        if script:
            job.script = script
        if command:
            job.command = command
        timeout_val = args.get("timeout", 0)
        if timeout_val:
            job.timeout = timeout_val
        if (
            agent
            or silent
            or approval_mode
            or model_arg
            or session_key
            or skip_dates
            or tz
            or persistent_session_explicit
            or minimal_context_explicit
            or hide_in_chat_explicit
            or strict_schedule_explicit
            or script
            or command
        ):
            svc._save()
        sched_str = format_schedule(job.schedule)
        sel().log_api_access(
            caller="mcp", operation="cron.create",
            outcome="allowed", source="mcp",
            resources=f"job_id={job.id}",
        )
        return f"Added job: {job.id} ({job.name}) [{sched_str}]. Tell the user: scheduled for {sched_str}."

    if name == "cron_update":
        jid = args["job_id"]
        kwargs: dict[str, Any] = {}
        for key in ("name", "message"):
            if key in args and args[key]:
                kwargs[key] = args[key]
        for key in ("agent", "channel", "thread_ts"):
            if key in args:
                val = args[key]
                if key == "thread_ts":
                    val = (val or "").strip() or None
                k = "agent_id" if key == "agent" else key
                kwargs[k] = val
        if "approval_mode" in args:
            kwargs["approval_mode"] = args["approval_mode"]
        if "silent" in args:
            kwargs["silent"] = args["silent"]
        if "skip_dates" in args:
            kwargs["skip_dates"] = args["skip_dates"]
        if "timezone" in args:
            tz_val = args["timezone"]
            if tz_val and tz_val not in available_timezones():
                safe_tz = redact(tz_val)
                return f"Error: invalid timezone: {safe_tz!r}"
            kwargs["timezone"] = tz_val
        if "strict_schedule" in args:
            kwargs["strict_schedule"] = args["strict_schedule"]
        if "persistent_session" in args:
            kwargs["persistent_session"] = args["persistent_session"]
        if "minimal_context" in args:
            mc = args["minimal_context"]
            if isinstance(mc, bool):
                kwargs["minimal_context"] = mc
        if "hide_in_chat" in args:
            hic = args["hide_in_chat"]
            if isinstance(hic, bool):
                kwargs["hide_in_chat"] = hic
        if "model" in args:
            m = str(args["model"] or "").strip()
            if m:
                # No membership gate: the model list is sourced from the live
                # kiro-cli `--list-models` (via /api/models), not the
                # claude_code registry family, so any id the CLI advertises is
                # valid. Matches the chat model path (which also skips
                # membership); the runtime is model-agnostic with a gateway
                # fallback. Only normalize the "auto" inherit sentinel.
                resolved_model = model_registry.to_provider_id(m, "claude_code")
                if resolved_model == "":
                    # "auto" sentinel — explicit inherit, same as clearing.
                    m = ""
            kwargs["model"] = m
        if "cron_expr" in args and args["cron_expr"]:
            kwargs["cron_expr"] = args["cron_expr"]
        if "every" in args and args["every"]:
            kwargs["every_secs"] = args["every"]
        if "timeout" in args:
            kwargs["timeout"] = args["timeout"]
        if not kwargs:
            return "Error: no fields to update"
        try:
            updated = svc.update_job(jid, **kwargs)
        except ValueError as e:
            return f"Error: {e}"
        if not updated:
            return f"Job not found: {jid}"
        sel().log_api_access(
            caller="mcp", operation="cron.update",
            outcome="allowed", source="mcp",
            resources=f"job_id={jid}",
        )
        sched_str = format_schedule(updated.schedule)
        return f"Updated job: {updated.id} ({updated.name}) [{sched_str}]"

    if name == "cron_remove":
        jid = args["job_id"]
        if svc.remove_job(jid):
            return f"Removed job: {jid}"
        return f"Job not found: {jid}"

    if name == "cron_remove_all":
        jobs = svc.list_jobs(include_disabled=True)
        if not jobs:
            return "No cron jobs to remove."
        session_key = _resolve_session_key()
        is_cli = os.environ.get("KIROCREW_CLI", "") == "1"
        if not is_cli:
            if not session_key:
                sel().log_tool_invocation(
                    session_key="mcp_cron",
                    source="mcp",
                    tool_name="cron_remove_all",
                    tool_kind="authz",
                    outcome="denied",
                    error="no session key set",
                )
                return "Error: no session key set; cannot determine job ownership."
            jobs = [j for j in jobs if j.session_key == session_key]
            if not jobs:
                return "No cron jobs owned by this session."
            sel().log_tool_invocation(
                session_key=session_key,
                source="mcp",
                tool_name="cron_remove_all",
                tool_kind="authz",
                outcome="scoped",
                resources=f"session={session_key} count={len(jobs)}",
            )
        else:
            sel().log_tool_invocation(
                session_key="mcp_cron",
                source="mcp",
                tool_name="cron_remove_all",
                tool_kind="authz",
                outcome="cli_admin",
                resources=f"count={len(jobs)}",
            )
        for j in jobs:
            svc.remove_job(j.id)
        return f"Removed {len(jobs)} job(s)."

    if name == "cron_pause":
        jid = args["job_id"]
        if svc.enable_job(jid, enabled=False):
            return f"Paused job: {jid}"
        return f"Job not found: {jid}"

    if name == "cron_resume":
        jid = args["job_id"]
        if svc.enable_job(jid, enabled=True):
            return f"Resumed job: {jid}"
        return f"Job not found: {jid}"

    if name == "cron_trigger":
        jid = args["job_id"]
        port = DASHBOARD_PORT
        secret_path = config_dir() / ".local_secret"
        ok, msg = trigger_cron_job(jid, port, secret_path)
        sel().log_api_access(
            caller="mcp", operation="cron.trigger",
            outcome="allowed" if ok else "error", source="mcp",
            resources=f"job_id={jid}",
        )
        if ok:
            return f"{msg} - executing now."
        return msg

    return f"Unknown tool: {name}"


def run_mcp_server() -> None:
    """Run MCP stdio server — reads JSON-RPC from stdin, writes to stdout."""
    run_mcp_stdio_loop("kirocrew-cron", "1.0.0", _list_tools, _call_tool)
