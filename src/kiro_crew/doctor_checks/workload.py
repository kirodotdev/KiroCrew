"""Workload rows of ``kirocrew doctor``: scheduled and queued work, and stall history.

Cron job health and the task queue are read off disk rather than from the
gateway's API, so they still answer when the gateway is the thing that is wedged.
The adaptive controller's caps and cap history are the one exception: they exist
only inside the gateway process, so they are read from the running gateway's
loopback API and reported as unread when it does not answer.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from kiro_crew import cli_doctor
from kiro_crew.doctor_checks import render

if TYPE_CHECKING:
    from kiro_crew.config import KiroCrewConfig


#: How recent a loop-stall dump stays a CURRENT issue whatever restarted after it.
#: File ordering alone cannot answer that question on the shipped `Restart=always`
#: unit (docs/guides/assets/kirocrew.service): the supervisor restarts a wedged
#: gateway within seconds, and the replacement's pre-created dump file supersedes
#: the stall almost immediately -- so a gateway wedging hourly would report every
#: stall as a past incident. Within a day a stall is still the operator's news,
#: restarts notwithstanding.
_STALL_CURRENT_SECS = 24 * 3600

#: Ceiling on the loopback read of the running gateway's controller snapshot,
#: the same bound the other doctor probes of the local gateway use: a wedged
#: gateway costs a short pause, never a hung diagnostic.
_ADAPTIVE_READ_TIMEOUT_SECS = 2

#: The loopback host the controller read dials. The local credential is
#: resolved for this same host, so it pairs with the listener actually reached.
_LOOPBACK_HOST = "127.0.0.1"


# ── cron job health ───────────────────────────────────────────────────────────
# The dashboard already surfaces a failing job per-row (an `err` badge on
# `last_status === 'error'`, with `last_error` on hover), and the gateway
# re-alerts on a still-failing job hourly. Both of those run INSIDE the
# gateway, so neither can speak when the gateway is the thing that is wedged.
# Doctor is a separate process the user runs by hand, which is why the scan
# reads `crons.json` off disk rather than asking the gateway's HTTP API: a check
# whose purpose is to survive a down gateway must not depend on one.
#
# It also covers a gap the dashboard has by construction: the status badge is
# rendered under an `enabled` guard, so a job that auto-paused shows only
# "paused" and its error state is not displayed at all.
#
# The scan itself lives in `cron.unhealthy_jobs_from_disk` so the pause-state
# predicates keep their single owner (`_record_user_paused` / `_record_is_enabled`
# in `cron_service/store.py`); this module owns only the presentation.
#
# Read-only, like the rest of doctor: an auto-paused job has failed
# `_AUTO_PAUSE_THRESHOLD` times in a row and is usually paused for a good
# reason, so silently resuming it during a diagnostic would hide the very
# problem the user ran doctor to find. The remediation is a `Fix:` hint naming
# a cron verb that already exists.
_CRON_REPORT_CAP = 5

#: How long past its ``next_run_at`` an unleased, non-terminal task row must be
#: before doctor calls it stranded. A due row still waits for a free slot, so the
#: bound leaves room for one queued behind a full cap; a row a lost terminal write
#: strands stays due for as long as nothing settles it, hours rather than minutes.
_STRANDED_OVERDUE_SECS = 3600

#: Stranded rows named one per line; the rest are summarised as ``+N more``.
_STRANDED_REPORT_CAP = 5


def _format_job_labels(entries: list[tuple[str, str]]) -> str:
    """Render ``(id, name)`` *entries* capped at :data:`_CRON_REPORT_CAP`.

    Beyond the cap the remainder is summarised as ``+N more``: a user with dozens
    of crons must not get a wall of text out of a diagnostic.

    Both fields go through :func:`_safe_display`. A job name is free text that an
    app or a hand-edit of the store can supply, so a name carrying OSC/ANSI
    controls must not be able to act on the terminal or spoof the surrounding
    diagnostic lines — the same reason the effective-model section escapes the
    values it reads off disk.
    """
    labels = [
        f"{render._safe_display(name)} ({render._safe_display(job_id)})" for job_id, name in entries
    ]
    if len(labels) <= _CRON_REPORT_CAP:
        return ", ".join(labels)
    shown = ", ".join(labels[:_CRON_REPORT_CAP])
    return f"{shown}, +{len(labels) - _CRON_REPORT_CAP} more"


def _doctor_task_store(issues: list[str]) -> None:
    """Report the durable task queue: depth, oldest wait, stranded rows, journal warnings.

    Reads ``$KIROCREW_HOME/tasks/tasks.db`` directly with a read-only view of
    the store's own diagnostics, so a wedged gateway cannot hide a backlog.
    Silent on a fresh install with no store yet. A network-filesystem data home
    is reported here because the store then runs on ``journal_mode=DELETE``,
    which is slower but never a refusal.

    A stranded row is one that is not terminal, holds no lease and fell due more
    than :data:`_STRANDED_OVERDUE_SECS` ago (:meth:`TaskStore.overdue_unleased_rows`).
    It is reported, never settled: doctor writes nothing to the store, and which
    ending a row earned is the boot reconcile's call, made from the run's own
    artifacts, not a diagnostic's.
    """
    from kiro_crew.config.paths import data_home
    from kiro_crew.taskq import TaskStore, TaskStoreUnavailable

    path = TaskStore.default_path(data_home())
    # A quarantined copy beside the live file is the boot-time verdict that the
    # previous store was corrupt: the gateway recreated it empty and moved the
    # damaged file here. Say so, once per copy, until the operator removes it.
    quarantined = sorted(path.parent.glob(f"{path.name}.corrupt-*")) if path.parent.exists() else []
    for copy in quarantined:
        if copy.name.endswith(("-journal", "-wal", "-shm")):
            continue
        print(f"  task store: ⚠️  a corrupt store was quarantined as {copy.name}")
        issues.append(
            f"task store {path} was found corrupt at a gateway boot and quarantined as "
            f"{copy}; work accepted into the old file was not recovered -- inspect or "
            "delete the quarantined copy"
        )
    if not path.exists():
        return
    store = TaskStore(path, diagnostic=True)
    stranded: list[Any] = []
    stranded_unread = ""
    try:
        store.open()
        lines = store.doctor_lines()
        by_state = store.count_by_state()
        try:
            stranded = store.overdue_unleased_rows(_STRANDED_OVERDUE_SECS)
        except (TaskStoreUnavailable, ValueError) as exc:
            # A row this build cannot model, or a read refused after the open:
            # the counts above still stand, so only this check is reported unread.
            stranded_unread = str(exc)
    except TaskStoreUnavailable as exc:
        print(f"  task store: ⚠️  {path} cannot be opened ({exc})")
        issues.append(
            f"task store {path} cannot be opened: accepted subagent work cannot be "
            "persisted or recovered until this is fixed"
        )
        return
    finally:
        store.close()
    for line in lines:
        print(f"  {line}")
    queued = {state: n for state, n in sorted(by_state.items()) if n}
    if queued:
        print("  task states: " + ", ".join(f"{state}={n}" for state, n in queued.items()))
    if stranded_unread:
        from kiro_crew.terminal_safe import safe_terminal_line

        print(f"  stranded rows: ⚠️  not checked ({safe_terminal_line(stranded_unread)})")
    elif stranded:
        _report_stranded_rows(stranded, store.now(), issues)
    for warning in store.warnings:
        issues.append(warning)


def _report_stranded_rows(rows: list[Any], now: float, issues: list[str]) -> None:
    """Name each row :meth:`TaskStore.overdue_unleased_rows` returned, capped.

    Each row is named by id, state, session key and how long it is overdue, the
    four things a user needs to find it in the store or match it to a chat slot.
    The id and session key are read off disk, so each goes through
    :func:`~kiro_crew.terminal_safe.safe_terminal_line` before it is printed.

    The verdict depends on whether a gateway runs, read from the lock-holder pair
    the Memory Pressure section composes (``cli_doctor._read_gateway_pid`` and
    ``resources._gateway_lock_indeterminate``). With none running, a due row may
    only be waiting for the next start, which claims it, so the rows are listed as
    advisory and no issue is recorded. With one running, or when the probe cannot
    tell, the rows are a warning and an issue.
    """
    from kiro_crew.doctor_checks import resources
    from kiro_crew.terminal_safe import safe_terminal_line

    hours = _STRANDED_OVERDUE_SECS / 3600
    try:
        gateway_down = (
            cli_doctor._read_gateway_pid() is None and not resources._gateway_lock_indeterminate()
        )
    except Exception:
        # The probe failed, which does not say the gateway is stopped: the
        # warning stands, as the Memory Pressure section's own "probe failed" does.
        gateway_down = False
    icon = "ℹ️ " if gateway_down else "⚠️ "
    print(
        f"  stranded rows: {icon} {len(rows)} task row(s) not terminal, with no lease, "
        f"due more than {hours:g}h ago"
    )
    for rec in rows[:_STRANDED_REPORT_CAP]:
        overdue_h = max(0.0, now - float(rec.next_run_at or 0.0)) / 3600
        print(
            f"               {safe_terminal_line(rec.id)} ({safe_terminal_line(rec.state)}, "
            f"session {safe_terminal_line(rec.session_key or '-')}, overdue {overdue_h:.1f}h)"
        )
    if len(rows) > _STRANDED_REPORT_CAP:
        print(f"               +{len(rows) - _STRANDED_REPORT_CAP} more")
    if gateway_down:
        print("               No gateway is running, so these may only be waiting for it:")
        print("               a gateway claims due rows when it starts. Run doctor again")
        print("               once it has been up a few minutes; a row still listed then")
        print("               is stranded.")
        return
    print("               No gateway holds these rows. A queued, retry_wait or recovering")
    print("               sub-agent row still counts as its parent session's pending")
    print("               work, so that session's chat slot answers")
    print("               409 slot_subagents_running until the row ends.")
    issues.append(
        f"{len(rows)} task row(s) not terminal, with no lease, due more than {hours:g}h ago"
    )


def _doctor_ledger_backlog() -> None:
    """Count the ledgers ``kirocrew ledger-sweep`` would list, and name that command.

    Advisory and read-only, so it takes no ``issues`` list: an old finished ledger
    is housekeeping, not a broken install, and an entry there would fail the
    doctor run. It runs the sweep's own :func:`ledger_sweep.scan` at the module's
    default window, so the count includes every entry the dry run lists: finished
    stores plus unreadable, linked or unaddressable stores listed for a human.
    It never purges. Per-ledger detail stays in the dry run. A ledger root that
    exists but cannot be read makes ``scan`` raise ``OSError``; that becomes one
    "could not check" row so the rest of the report still runs.
    """
    from kiro_crew import ledger_sweep
    from kiro_crew.terminal_safe import safe_terminal_line

    window = f"{ledger_sweep.DEFAULT_OLDER_THAN_DAYS:g}-day window"
    print("\nLedgers")
    try:
        report = ledger_sweep.scan()
    except OSError as exc:
        print(f"  ledgers:     ⚠️  could not check: {safe_terminal_line(str(exc))}")
        return
    count = len(report.candidates)
    if not count:
        print(f"  ledgers:     ✅ kirocrew ledger-sweep lists nothing ({window})")
        return
    print(
        f"  ledgers:     🧹 kirocrew ledger-sweep lists {count} ledger(s) ({window})"
        " -- run it to review them"
    )


def _doctor_overload_resilience(cfg: KiroCrewConfig) -> None:
    """Print the overload-resilience contract this install runs under.

    Configuration and static platform facts, plus one live block: the running
    gateway's adaptive-controller snapshot (:func:`_doctor_adaptive_live`),
    read from the gateway rather than computed here. The live gate counts and
    the per-scope dependency schedules stay gateway-process state, served by
    ``GET /api/sessions/health``, and a doctor process does not print them.
    What it CAN state is the bound each mechanism is configured to (so a stuck
    queue can be read against its budget) and which liveness evidence this
    host's platform provides.
    """
    from kiro_crew.cold_start_sizing import describe_cold_start_limits
    from kiro_crew.recovery.ladder import configure_default_ladder
    from kiro_crew.session_start_sizing import resolve_session_start_sizing

    agent = cfg.agent
    gw = cfg.mcp_gateway
    # Doctor is its own process: "auto" here is what this host sizes to NOW. The
    # width a running gateway's gate uses is its boot log line.
    try:
        sizing = resolve_session_start_sizing(agent.session_start_concurrency)
        start_width = sizing.describe()
        if sizing.host is not None:
            start_width += " [probed now; running gateway: boot log]"
    except Exception:
        start_width = f"{agent.session_start_concurrency} (unresolved)"
    print(
        "  admission: session_start_concurrency="
        f"{start_width} "
        f"{describe_cold_start_limits(agent)} "
        f"spawn_gate={gw.spawn_concurrency_initial} "
        f"[{gw.spawn_concurrency_min}..{gw.spawn_concurrency_max}] "
        f"queue_wait={gw.spawn_queue_wait_secs}s "
        f"dispatch_window={agent.task_dispatch_window} "
        f"(live gate counts: GET /api/sessions/health)"
    )
    mode = agent.adaptive_concurrency_mode if agent.adaptive_concurrency else "off"
    # The execution cap starts at its ceiling; adaptive_initial is inert.
    print(
        f"  adaptive concurrency: {mode} floor={agent.adaptive_floor} "
        f"sample={agent.controller_sample_secs}s"
    )
    _doctor_adaptive_live()
    print("  recovery ladder:")
    # Through the boot seam a gateway uses, on this process's own ladder: these
    # rows are the CONFIGURED schedule, so they cannot disagree with the
    # dependency-wait line below, which reads the same two keys. Still config
    # only — a doctor process has no live attempt count to show.
    for row in configure_default_ladder(cfg).table():
        print(
            f"    {row['layer']}: backoff {row['backoff_base_secs']:g}s→"
            f"{row['backoff_max_secs']:g}s, {row['attempts_before_escalation']} attempts → "
            f"{row.get('escalates_to') or 'notify'}"
        )
    print(
        "  dependency waits: backoff "
        f"{agent.recovery_backoff_base_secs:g}s→{agent.recovery_backoff_max_secs:g}s "
        "(the shared recovery schedule), "
        f"max_attempts={agent.dependency_max_attempts}, "
        f"deadline={agent.dependency_wait_deadline_secs}s"
    )
    print(f"  interactive commands: policy={agent.interactive_command_policy}")
    print(
        "  uncharged residency: native children (kiro-cli use_subagent / KAS subtasks) "
        "are counted on the parent session, never a budget slot, lane slot or task row "
        '(live count: GET /api/sessions/health "uncharged")'
    )
    print(f"  liveness evidence: {_liveness_platform_line()}")


def _gateway_adaptive_state() -> tuple[int, dict[str, Any] | None, str]:
    """Read the running gateway's adaptive-controller snapshot.

    Returns ``(port, state, why)``. *state* is the snapshot the controller's
    ``state()`` builds (mode, caps against their ceilings, ``last_cut`` with
    its ``age_secs``, ``recent_decisions`` newest last), or ``None`` with *why*
    naming what stopped the read.

    The controller exists only in the gateway process, so the snapshot comes
    from ``GET /api/spawn/adaptive``: the route the gateway serves for exactly
    this out-of-process reader, under the internal-API prefix a loopback
    ``X-Internal-Secret`` caller reaches. The credential is the local secret
    resolved for the loopback host this request dials, and the send goes through
    ``cli_server_peer._owner_verified_secret_urlopen`` like every other CLI
    sender of it: the gateway's unix socket when it is present, TCP loopback
    otherwise, with the connected peer proven to be this install's recorded
    gateway before the first byte. Without a credential nothing is sent at all.
    Read-only: a GET, and the route only reads the controller.
    """
    from kiro_crew.cli_server_peer import _GatewayPeerRefused, _owner_verified_secret_urlopen
    from kiro_crew.config.loader import read_local_secret
    from kiro_crew.port_resolution import resolve_client_port

    port = resolve_client_port(None)
    secret = read_local_secret(port, dial_host=_LOOPBACK_HOST)
    if not secret:
        return port, None, "no local gateway credential for this port (gateway not running?)"
    req = urllib.request.Request(
        f"http://{_LOOPBACK_HOST}:{port}/api/spawn/adaptive",
        headers={"X-Internal-Secret": secret},
    )
    try:
        with _owner_verified_secret_urlopen(
            req, port, _ADAPTIVE_READ_TIMEOUT_SECS, operation="doctor_adaptive_read"
        ) as resp:
            payload = json.loads(resp.read())
    except _GatewayPeerRefused:
        return port, None, "the answering process is not proven to be this install's gateway"
    except urllib.error.HTTPError as exc:
        return port, None, f"the gateway refused the read (HTTP {exc.code})"
    except (urllib.error.URLError, OSError) as exc:
        # Only a bare read timeout (the connection was accepted and the gateway
        # then stalled -- a loop thread starved of the GIL) means it is up but
        # busy. A connect-phase failure is reported as no answer: this read uses
        # a 2 s deadline, which on native Windows coincides with the SYN-retry
        # before a closed loopback port reports a refusal, so a connect timeout
        # here must not read busy. The non-doctor sites, whose deadlines sit past
        # that window, use loopback_http.deadline_expired.
        if isinstance(exc, TimeoutError):
            return port, None, "the gateway is busy (up but did not answer)"
        return port, None, "no gateway answered"
    except ValueError:
        return port, None, "the gateway's answer was not JSON"
    state = payload.get("adaptive") if isinstance(payload, dict) else None
    if not isinstance(state, dict):
        return port, None, "the gateway's answer carried no controller snapshot"
    if not state:
        return port, None, "the running gateway has no adaptive controller"
    return port, state, ""


def _doctor_adaptive_live() -> None:
    """Print the running gateway's adaptive controller: caps, last cut, cap history.

    The configured line above it states the bounds; this states what the
    controller did inside them: the mode, the execution cap and the spawn gate
    against their ceilings, the last pressure cut with its signals and age
    while the execution cap is below its ceiling, the last decision, and the
    recent cap changes, newest last, each stamped with the ``gateway.log``
    clock so it can be matched against the log line it produced.

    Rendered by ``resource_status.adaptive_summary_lines``, the renderer the
    ``resource_status`` tool prints the same snapshot with, so the two surfaces
    cannot describe one controller two ways. Every line goes through
    :func:`~kiro_crew.terminal_safe.safe_terminal_line`, since it arrives over
    a socket. Advisory: an unread state is one line and never an issue, because
    the Connectivity section already reports a gateway that is down.
    """
    from kiro_crew.resource_status import adaptive_summary_lines
    from kiro_crew.terminal_safe import safe_terminal_line

    try:
        port, state, why = _gateway_adaptive_state()
    except Exception as exc:
        print(f"  adaptive controller: not read (check failed: {safe_terminal_line(str(exc))})")
        return
    if state is None:
        print(f"  adaptive controller: not read on port {port}: {why}")
        return
    print(f"  adaptive controller (running gateway on port {port}):")
    # The renderer's first line is its own heading; the line above replaces it
    # with the source of the snapshot.
    for line in adaptive_summary_lines(state)[1:]:
        print(f"  {safe_terminal_line(line)}")


def _liveness_platform_line() -> str:
    """Which stall evidence this platform's liveness oracle can produce.

    Mirrors the platform matrix in ``acp/liveness.py``: a missing column is a
    DECLARED degradation (bounded by the no-progress budget), never a stall
    the oracle silently calls WORKING.
    """
    if sys.platform.startswith("linux"):
        return (
            "linux /proc — process tree, CPU+IO movement, STUCK_INPUT (blocked "
            "tty/pipe read), established-flat sockets: full matrix"
        )
    if sys.platform == "darwin":
        return (
            "macOS libproc — process tree and CPU-only movement; STUCK_INPUT and "
            "socket evidence absent (a live but flat shell child reads UNKNOWN "
            "platform_limited and is bounded by the no-progress budget)"
        )
    if sys.platform.startswith("win"):
        return (
            "windows — no process-tree backend; shell and MCP tool calls read "
            "UNKNOWN platform_limited and are bounded by the no-progress budget"
        )
    return f"{sys.platform} — no process-tree backend; UNKNOWN platform_limited"


def _doctor_cron_quarantine(issues: list[str]) -> bool:
    """Name every copy the gateway moved aside, and how to put it back.

    The gateway renames an unparseable ``crons.json`` at startup so it can run
    on an empty schedule. The jobs in the copy do not run until the user
    restores it. Returns whether it printed the section header.
    """
    store, copies = cli_doctor.cron_store_quarantine_copies()
    if not copies:
        return False
    print("\nCron Jobs")
    print(f"  quarantine:  ⚠️  {len(copies)} unreadable schedule file(s) moved aside")
    for copy in copies:
        print(f"               {render._safe_display(str(copy))}")
    print("               `crons.json` was not a readable schedule (invalid JSON,")
    print("               or no `jobs` list), so the gateway moved it aside")
    print("               unchanged and started with no scheduled tasks.")
    print("               Jobs in the copy do not run.")
    print("               Restore: fix the copy, run `kirocrew stop`,")
    print(f"               move the copy back to {store},")
    print("               then start the gateway. That replaces any task added")
    print("               since. Delete the copy once you no longer need it.")
    issues.append(f"{len(copies)} unreadable cron store copy(ies) set aside")
    return True


def _doctor_cron_health(issues: list[str]) -> None:
    """Report cron jobs that auto-paused or last ran with an error.

    Silent on a healthy store — and on a fresh install with no ``crons.json`` at
    all — so a normal doctor run gains no noise. Speaks only when there is
    something the user can act on.

    A store that EXISTS but cannot be read is one of those things, and is
    reported even though the scan returns nothing: the scheduler can load no
    jobs from it, so every job has stopped. Staying silent there would hand
    back a clean bill of health in precisely the state this check exists to
    surface. The runtime readers keep degrading quietly; only this diagnostic
    speaks up.
    """
    header_printed = _doctor_cron_quarantine(issues)
    auto_paused, errored, loadable = cli_doctor.unhealthy_jobs_from_disk()
    if not auto_paused and not errored:
        # The flag rides the scan's own read, so `crons.json` is opened ONCE per
        # doctor run. False means the store is present and the scheduler can
        # load nothing from it; a missing store and an honestly empty one both
        # report True and stay silent.
        if not loadable:
            if not header_printed:
                print("\nCron Jobs")
            print("  store:       ⚠️  `crons.json` exists but could not be read")
            print("               No jobs can be loaded from it, so every scheduled")
            print("               job has stopped. The scheduler logs the parse error")
            print("               on startup.")
            print("               Fix: restore it from a snapshot (`kirocrew restore`)")
            print("               or move it aside to start with an empty schedule.")
            issues.append("cron store unreadable")
        return

    if not header_printed:
        print("\nCron Jobs")
    if auto_paused:
        print(f"  auto-paused: ⚠️  {len(auto_paused)} job(s) paused after repeated failures")
        print(f"               {_format_job_labels(auto_paused)}")
        print("               A job auto-pauses after consecutive failures and stays")
        print("               paused across restarts. Check why it failed before")
        print("               resuming it — the pause is usually load-bearing.")
        print("               Fix: `kirocrew cron resume <id>` once the cause is fixed.")
        issues.append(f"{len(auto_paused)} cron job(s) auto-paused")
    if errored:
        print(f"  errored:     ⚠️  {len(errored)} job(s) last ran with an error")
        print(f"               {_format_job_labels(errored)}")
        print("               If it has a repeating schedule, the next run may recover")
        print("               on its own; a one-shot job has no next run.")
        print("               Fix: `kirocrew cron trigger <id>` to retry now. The recorded")
        print("               error text is shown on the dashboard's Schedule page.")
        issues.append(f"{len(errored)} cron job(s) last ran with an error")


def _doctor_crash_dumps(issues: list[str]) -> None:
    """Render the ``Loop-stall Crash Dumps`` section: the newest dump with stacks,
    where the loop wedged, and who it was working for."""
    print("\nLoop-stall Crash Dumps")
    try:
        dumps_dir = cli_doctor.get_dumps_dir()
        _latest = cli_doctor.newest_dump_with_stacks(dumps_dir)
        if _latest is not None:
            _age_s = cli_doctor.dump_age_seconds(_latest)
            # Every gateway start pre-creates its own dump file, so a dump WITH
            # stacks that a later local session's header file sits after was
            # written by a session another one has already replaced without
            # wedging. That is a past incident: counting it as something to fix
            # makes `doctor` report a fault for a week after one stall, on a
            # gateway that has been healthy the whole time — and the real
            # finding in that run gets read as one more line of the same noise.
            #
            # Ordering alone is not enough to conclude it, though. Under the
            # shipped `Restart=always` unit a wedged gateway is replaced within
            # seconds, so the successor supersedes the stall almost at once and
            # a gateway wedging hourly would downgrade every stall forever —
            # under-reporting exactly the chronic case an operator needs. Two
            # further terms keep that case visible: a stall inside
            # `_STALL_CURRENT_SECS` is still current news whatever restarted
            # since, and two or more stalls on record is a gateway wedging
            # repeatedly, which no amount of successful restarting makes
            # historical.
            #
            # The stacks are printed either way, because they are what anyone
            # investigating that stall needs; only the issue verdict changes.
            _stalls_on_record = cli_doctor.dumps_with_stacks(dumps_dir)
            _superseded = (
                cli_doctor.dump_superseded(_latest, dumps_dir)
                and _age_s >= _STALL_CURRENT_SECS
                and _stalls_on_record < 2
            )
            if _age_s < 7 * 86400:  # Less than 7 days old
                _age_h = _age_s / 3600
                _icon = "ℹ️ " if _superseded else "⚠️ "
                print(f"  last dump:   {_icon} {_latest.name} ({_age_h:.1f}h ago)")
                if _superseded:
                    print(
                        "               a later gateway session started after it and did "
                        "not wedge — past incident, not a current fault"
                    )
                # 8 lines = preamble + thread header + ~6 frames: enough to
                # reach past the asyncio plumbing into the Kiro Crew frame
                # that identifies WHERE the loop wedged.
                _stack = cli_doctor.dump_first_stack_lines(_latest, max_lines=8)
                if _stack:
                    print("  MainThread stuck at:")
                    for _line in _stack:
                        print(f"    {_line}")
                # Who the loop was working for. Read from the dump's wedged
                # stack and the cron in-flight markers on disk -- no gateway
                # needed -- and phrased as evidence plus the one action it
                # supports, or the statement that it supports none.
                _attribution = cli_doctor.attribute_dump(_latest, cli_doctor.config_dir())
                print("  attribution:")
                for _line in cli_doctor.describe(_attribution):
                    print(f"    {_line}")
                # Same predicate as the breaker and describe(): a lone marker
                # under a chat/Slack stack is a bystander, not the culprit.
                if _attribution.is_cron and _attribution.job is not None:
                    _paused_job = cli_doctor.job_pause_state_from_disk(_attribution.job.job_id)
                    if _paused_job is not None:
                        print(f"    job is currently {_paused_job}")
                    if not _superseded:
                        issues.append(
                            "loop-stall dump attributed to cron job "
                            f"{render._safe_display(_attribution.job.name)} "
                            f"({render._safe_display(_attribution.job.job_id)})"
                        )
                if not _superseded:
                    issues.append(f"recent loop-stall crash dump ({_age_h:.0f}h ago)")
            else:
                print(
                    f"  last dump:   ✅ oldest only ({_age_s / 86400:.0f}d ago, no recent stalls)"
                )
        else:
            print("  dumps:       ✅ no crash dumps found (healthy)")
        print(f"  dump dir:    {dumps_dir}")
    except Exception as exc:
        print(f"  crash dumps: ⚠️  check failed ({exc})")
