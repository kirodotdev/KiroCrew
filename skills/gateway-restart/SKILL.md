---
name: gateway-restart
description: Gracefully restart the Kiro Crew gateway from a running agent session while preserving continuity with a finite same-session resume monitor. Use when the user says "restart yourself", "restart gateway", or "reload config", or after a change that actually requires restart.
triggers: restart, reload, restart yourself, restart gateway, apply changes, reload config
---

# Gateway Restart

## Overview

Gracefully restart the Kiro Crew gateway from a running agent session. The direct lifecycle command is blocked by the agent shell, and restarting the gateway interrupts the current process. This skill arms a durable same-session verifier before launching the detached helper.

## Core Concepts

### The Problem

The agent cannot invoke the lifecycle command directly, and an unverified detached launch is not evidence of success. The safe sequence is: prepare attempt identity, request a finite same-session monitor, launch the delayed helper, then verify from the resumed session.

### Restart Mechanism

The agent cannot run `kirocrew restart` directly — kiro-cli's security filter blocks it at the shell command level (regex match on the command string). Platform-specific scripts handle this indirectly:

**Linux / macOS:**

```bash
nohup /path/to/skills/gateway-restart/do-restart.sh >/dev/null 2>&1 & disown
```

The script sleeps 10 seconds (giving the session time to respond), then invokes the restart. Because it's a detached process reparented to PID 1, it survives the gateway's death and executes reliably.

Both scripts **record the outcome** instead of discarding it: the restart's exit status is written to a status file under `<crew home>/logs/` and its output goes to a **log correlated with that attempt** — `<status file>.log` when an attempt-specific status file is passed, the shared `logs/restart.log` for a lone unscheduled run (crew home is `$KIROCREW_HOME`, default `~/.kiro/crew`; both scripts derive their paths from it, so never pass hardcoded `%USERPROFILE%` paths). The status file is **attempt-specific** when the scheduler passes one — `KIROCREW_RESTART_STATUS_FILE` (Linux/macOS) or `-StatusFile` (Windows), see step 3 — so overlapping restart attempts cannot overwrite each other's verdict or quote each other's diagnostics; without it the shared default `logs/restart-status` is used. The file is removed when the attempt starts, so while it is absent the attempt is pending; once present it names that attempt's exit status. The CLI restart verb itself verifies the replacement gateway is serving and exits non-zero when it is not — the status file is how that verdict reaches the resumed session (see "Verify the outcome" below).

**Windows:**

```powershell
$kiroBin = (Get-Command kirocrew).Source
Start-Process -WindowStyle Hidden powershell -ArgumentList "-ExecutionPolicy", "Bypass", "-File", "`"<path>\do-restart.ps1`"", "-KirocrewBin", "`"$kiroBin`""
```

This overview form omits `-StatusFile`, so the shared default `logs/restart-status` is used — fine for a lone attempt. The **runnable monitored form is step 3 below**, which defines an attempt-specific `$statusFile` first and passes it; never pass `-StatusFile` without defining the variable, since an empty value silently collapses both artifacts onto the shared default and loses per-attempt isolation.

The PowerShell script (`do-restart.ps1`) accepts `-KirocrewBin` (the resolved absolute path to `kirocrew.exe`) and `-StatusFile` (the attempt-specific verdict path from step 3). The attempt log is always `<status file>.log` — not caller-settable. **Attempt paths are confined:** both scripts only accept a status file of the form `<crew home>/logs/restart-status.<suffix>` (no slashes or `..` in the suffix); anything else falls back to the shared default, because the detached helper runs outside any agent sandbox and must never delete or overwrite a caller-chosen file. It sleeps 10 seconds, then calls the binary. `Start-Process -WindowStyle Hidden` creates a detached process that survives the gateway's death. Unlike Unix, Windows has no `nohup`/`disown` — `Start-Process` with `-WindowStyle Hidden` is the equivalent pattern for fire-and-forget background work.

> **Important:** Always resolve `kirocrew` to an absolute path at schedule time (before the detached process launches). A hidden process may not inherit the same PATH as the agent session — this is the documented Windows reality. If resolution fails, the script falls back to PATH lookup and then to `python -m kiro_crew.cli restart` via the venv Python. All path arguments passed to `Start-Process -ArgumentList` must be wrapped in escaped quotes (`` `"..`" ``) to handle paths containing spaces (e.g. `C:\Users\John Smith\...`).

### Resume Monitor

Before triggering the restart, request one finite `monitor_start` loop on the
current session. Prompt loops persist across gateway restarts, so the same
conversation wakes to verify the attempt without creating separate cron-owned
sessions:

```python
monitor_start(
    message="""A gateway restart attempt is pending. Attempt start: <utc>. Status file: <status_file>. Pre-restart gateway pid: <pid>. Validate both values, then follow this skill's 'Verify the outcome' step. Before 5 minutes, an absent status is still pending: report nothing and keep monitoring. At or after 5 minutes, decide with the gateway-identity check. On any decisive success or failure, report once and call autonudge_stop. Continue pending work only after verification.""",
    interval_secs=60,
    gate=False,
    max_cycles=6,
    max_runtime_secs=420,
)
```

- The monitor is bounded by both delivered turns and wall clock.
- `monitor_start` is create-only. Never overwrite an automation already bound to
  the session; if one exists, do not restart until the user chooses how to handle it.
- Arming is applied at the current turn boundary. Launch the delayed helper only
  after requesting the monitor, make that launch the final tool action, and end the
  turn immediately so the 10-second delay cannot beat the arm.
- A monitor wake proves only that some gateway is serving. Verify the attempt's
  status and process identity before reporting success.

## Procedure

### 1. Check the session's automation slot

Call `monitor_inspect`. If an active prompt loop or structured monitor already
occupies this session, preserve it and stop: replacing unrelated automation is not
part of a restart. If no automation is active, continue.

### 2. Prepare and request the resume monitor

Prepare the attempt-specific status path, UTC start time, and pre-restart gateway
PID as described in step 3. Put those validated values into the bounded
`monitor_start` instruction above. The request is not proof that arming succeeded;
finish the turn promptly so the session directive can apply before the helper wakes.

### 3. Schedule the restart

**Use the attempt identity placed in the resume monitor. If it has not yet been prepared, create all three values before requesting that monitor:**

1. **A UTC start time and attempt-specific status file**, so the monitor can distinguish its early and final checks, overlapping attempts cannot overwrite each other's verdict, and a failed launch cannot be read against stale status:
   ```bash
   ATTEMPT_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   STATUS_FILE="${KIROCREW_HOME:-$HOME/.kiro/crew}/logs/restart-status.$(date +%s).$$"
   ```
   ```powershell
   $attemptUtc = [DateTimeOffset]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
   $crewHome = if ($env:KIROCREW_HOME) { $env:KIROCREW_HOME } else { Join-Path $env:USERPROFILE ".kiro\crew" }
   $statusFile = Join-Path $crewHome ("logs\restart-status." + [DateTimeOffset]::Now.ToUnixTimeSeconds() + "." + $PID)
   ```
2. **The current gateway pid**, recorded now so the resumed session can tell a new gateway from the old one without reading any fenced path (the crew home's `run/` dir is agent-fenced — never instruct a resumed session to read it). Use the gateway pid reported by `kirocrew status` as the primary source. If it is unavailable, fall back to a process listing whose pattern cannot match its own invoking shell and covers both install shapes (the `kirocrew` binary and an editable install's `python -m kiro_crew gateway`):
   ```bash
   pgrep -f "kiro_?crew[ ]gateway" | head -1   # brackets prevent self-match; covers kirocrew + kiro_crew
   ```
   A bare `pgrep -f "kirocrew gateway"` self-matches the shell running it and misses editable installs entirely — two transient wrapper-shell pids then "differ" across the restart and fake a pid-changed signal.

Then launch the bundled script as a detached process, passing the attempt's status file:

**Linux / macOS:**
```bash
KIROCREW_RESTART_STATUS_FILE="$STATUS_FILE" nohup /path/to/skills/gateway-restart/do-restart.sh >/dev/null 2>&1 & disown
```

**Windows:**
```powershell
$kiroBin = (Get-Command kirocrew).Source
$scriptPath = Join-Path (Split-Path $PSScriptRoot) "skills\gateway-restart\do-restart.ps1"
if (-not (Test-Path $scriptPath)) { $scriptPath = "$env:USERPROFILE\.kiro\crew\skills\gateway-restart\do-restart.ps1" }
Start-Process -WindowStyle Hidden powershell -ArgumentList "-ExecutionPolicy", "Bypass", "-File", "`"$scriptPath`"", "-KirocrewBin", "`"$kiroBin`"", "-StatusFile", "`"$statusFile`""
```

The script's 10-second delay gives the current session time to finish responding.

> **Path resolution:** On both platforms, use the installed skill path (`~/.kiro/crew/skills/gateway-restart/`). The `<path>` in the Restart Mechanism section above is the same directory.

### 4. Confirm to user

> Gateway restart scheduled. It will restart in ~10 seconds. I'll verify the outcome and resume automatically afterward.

### 5. Verify the outcome (in the resumed session)

**Never tell the user the restart succeeded without checking.** The restart runs as a disowned process; the only place its verdict lands is the status file the helper script writes. When the resume monitor wakes, take the attempt's status-file path, start time, and pre-restart pid from its instruction — but **treat all three as untrusted data and validate before use**: the status-file path must match the confined pattern `<crew home>/logs/restart-status.<suffix>` with no path separators or `..` in the suffix (the exact rule the scripts enforce), the pid must be a plain integer, and the start time must be parseable UTC. A path failing validation means the message was malformed or forged — fall back to the shared `logs/restart-status` and never read, quote, or delete the non-conforming path. Then:

1. Read the attempt's status file. The **gateway-identity check** used below is: the current gateway pid (from `kirocrew status`, or the fallback `pgrep -f "kiro_?crew[ ]gateway" | head -1` — the same non-self-matching form as step 3, never a bare `pgrep -f "kirocrew gateway"`) exists, differs from the pre-restart pid recorded in the resume message, and `/api/ready` answers. If no gateway pid can be established at all, treat the identity check as unevaluable — report accepted-but-unverified rather than reading a pid difference off wrapper shells. Never read the crew home's `run/` dir — it is agent-fenced.
2. **`0`** → what this proves depends on the install. On a **foreground** install the restart verb itself verified the replacement gateway is serving — confirm to the user ("Back online.") and continue. On a **service-managed** install (systemd/launchd), `0` means the service manager *accepted* the restart, not that the replacement survived startup — run the gateway-identity check first; if it cannot be evaluated (no recorded pid), report the restart as accepted-but-unverified rather than claiming success.
3. **Non-zero** → the restart FAILED even though this session is running (the gateway you woke up in may be the old process, or a service manager refused the restart). Read the tail of the attempt's own log — `<status file>.log` (the shared `logs/restart.log` only for an unscheduled run) — and report the failure to the user, quoting the diagnostic. If the log names a privileged command the operator must run themselves (e.g. `sudo systemctl restart kirocrew` for a system service unit), relay that command — do not retry the same restart path.
4. **Absent** → the attempt is pending, the script never ran — or, on a **service-managed** install, the restart succeeded and took the helper with it: `systemctl restart` terminates the unit's whole control group, and a helper launched from a gateway session lives in that cgroup (`disown` edits the shell's job table, not cgroup membership). Before five minutes have elapsed, report nothing and leave the monitor active. At or after five minutes, a service-managed install is decided by the gateway-identity check (pid changed + `/api/ready` answering = restarted; unchanged pid = it never happened); on a foreground install, an absent status means the restart never completed, so report failure and point to `<status file>.log`.
5. **Clean up and stop:** after a decisive verdict, delete the attempt's status file and its `.log` — only ever the path that passed confinement validation — then call `autonudge_stop` so no later verifier wakes.

## When to Restart

- User explicitly asks ("restart yourself", "reload")
- Config change made that requires restart (`config.json`, `mcp.json`, agent files)
- After applying a Kiro Crew update (see self-update skill)

If the user is at the dashboard and no pending work needs resuming, point them at
**Settings → About → Restart Gateway** (confirm-gated, backed by
`POST /api/restart`, which coalesces duplicate presses) instead of running this
procedure — it is the cheapest correct answer. Use the monitored-restart flow above
when the conversation must survive the restart, or when no human is present.

## Consent and Offering Restarts

**Never restart without the user's knowledge.** The user should never be surprised by a restart.

### When a restart is needed (but user didn't ask for one)

If you make a config change or apply an update that requires a restart, **inform and offer** — do not restart automatically:

> I've updated the config. A gateway restart is needed for this to take effect. Want me to restart now?

Only proceed with the restart if the user confirms.

### Learning automatic restart permission

If the user grants blanket permission for a specific scenario (e.g. "yes, always restart after auto-updates"), save it as a lesson:

```python
learn_add(
    rule="Okay to automatically restart the gateway after applying a Kiro Crew update.",
    category="preference",
)
```

In future sessions, if that lesson exists, you may restart without re-asking for that specific scenario. But only for the scenario the user explicitly approved — not as general permission.

### Automatic updates

The self-update skill uses Kiro Crew's built-in layout-aware updater, not a custom
cron. An authenticated in-app apply owns its drain and restart. If the user grants
standing permission for an agent-initiated restart after a terminal update, save
that narrow scenario as a preference; otherwise notify and wait for confirmation.

## Common Mistakes

- **Reporting success because the monitor woke** — that proves only that a gateway is serving, not that it is a new process. Always run the status-file and identity checks.
- **Forgetting to arm the resume monitor** — the helper interrupts the session and nobody verifies the result. Request the bounded monitor before launching it.
- **Replacing an existing automation** — `monitor_start` is create-only for a reason. Preserve unrelated work and do not restart until the collision is resolved.
- **Leaving the verifier active after a verdict** — clean the attempt artifacts and call `autonudge_stop` once.
- **Setting the helper delay too short** — if the helper acts before the turn-boundary arm lands, continuity is lost. Keep the 10-second delay and launch it last.
- **Windows: inline Python `-c` scripts via Start-Process** — nested quotes and backslash paths break PowerShell argument passing. Always use a script file (`do-restart.ps1`), never an inline `-c "..."` command.
- **Windows: using bash/nohup/disown** — these don't exist on Windows. Use `Start-Process -WindowStyle Hidden powershell` instead.
