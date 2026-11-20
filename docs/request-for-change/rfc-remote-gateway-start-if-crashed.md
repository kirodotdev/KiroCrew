---
title: "Remote gateway start after a crash: an opt-in, start-only self-heal step for crews with no service manager"
status: draft
author: neilieva
created: 2026-11-20
last-audited: 2026-11-20
audited-at: 241e41efc2
doc-pr: null
implementation-prs: []
tracking-issues: [39180]
supersedes: []
superseded-by: []
---

# RFC: Remote gateway start after a crash

**Status:** `draft`. Nothing here is built. Measured against main `241e41efc2`.

## 1. Summary

When a remote crew's gateway dies and its host stays up, the hub's self-heal rebuilds the tunnel, re-mints the token, gives up after about six minutes and reports "SSH works but the remote Kiro Crew dashboard isn't responding (is the remote gateway running?)". Nothing starts the gateway again. A gateway under systemd or launchd is started again by its service manager. One run in the foreground, under `nohup` or `tmux`, or in a container with no init system stays down until a person logs in.

This RFC adds a per-crew opt-in, off by default, for that second case. When self-heal gives up and the diagnosis says the transport works but the dashboard does not answer, the hub sends one fixed command, `kirocrew start --if-crashed`, at most once per outage. The command runs on the remote and starts a gateway only when no service manager owns it, nothing is running, and the newest crash dump shows a crash or a loop stall. It never stops anything. Every other case, including a clean stop, a gateway that is running but hung, and a remote too old to know the command, starts nothing and says why in the tunnel status.

The maintainer accepted this direction on [#39180](https://github.com/kirodotdev/KiroCrew/issues/39180) and asked the RFC to cover two things: how a remote too old to report its last exit fails closed (§5.6, §7), and what the tunnel status shows for each outcome (§5.7).

## 2. Motivation

### Current state (at `241e41efc2`)

- Self-heal starts nothing on the remote. `_recover` in `instances/ssh_tunnel_manager_self_heal.py` rebuilds the tunnel (tier 1) and re-mints the token (tier 2). After `max_recovery_attempts` (default 8, `DEFAULT_MAX_RECOVERY_ATTEMPTS` in `instances/constants.py`) it logs "Giving up self-heal", calls `_schedule_diagnosis` and tries again every `_GIVE_UP_RETRY_SECS` (300 s) until `_RECOVER_WINDOW_SECS` (one hour) has passed since `_recover_since` was set. `docs/system-specs/modules/instances.md` §4 puts the handoff to diagnosis at about six minutes at the defaults. `_on_tunnel_healthy` clears `_recover_attempts` and `_recover_since` when a probe answers end to end.
- The diagnosis already names this case. `diagnose` in `instances/ssh_tunnel_manager_remote_ops.py` stores its result on `TunnelStatus.diagnosis`. For a dead gateway on a reachable host the code is `REMOTE_DOWN` (`instances/diagnostics.py`), with the reason quoted above.
- A service manager already starts a managed gateway again. The systemd unit from `render_unit` (`service/linux_unit.py`) has `Restart=always`, `RestartSec=10` and a start limit (`StartLimitBurst`, `StartLimitIntervalSec`). The launchd plist from `render_plist` (`service/macos.py`) has `KeepAlive`. The gap is only gateways no manager owns.
- Main can tell a clean stop from a crash. Every dashboard start pre-creates a header-only dump in `<data home>/logs/crash-dumps/` (`open_dump_file` in `dashboard/crash_dump_store.py`). Every orderly exit leaves an empty `<dump>.clean` beside it as its last step (`record_clean_exit`, reached through `mark_clean_exit` from `_shutdown_and_exit` in `slack/gateway_runtime/exit_path.py`, from the update restart in `dashboard/handlers/updates_apply.py` and from the Slack restart in `slack/events_slash.py`). `kirocrew stop` only sends SIGTERM (`_stop` in `cli_server_stop.py`), which runs that shutdown. The loop-stall watchdog writes thread stacks into the same file and then ends the process (`dashboard/loop_watchdog.py`). `unexplained_exits` already reads these files for `kirocrew doctor`.
- Neither existing remote verb is safe to send unattended. Bare `kirocrew restart` (`_restart` in `cli_server_restart.py`) stops whatever listens and then spawns, so on a hung but live gateway it is the restart the [2026-11-10 ruling](../decisions/2026-11-10-updates-never-restart-a-running-gateway.md) forbids. `RESTART_WHEN_IDLE_SUBCOMMAND` (`instances/remote_update.py`) restarts a gateway that is up and idle, which a tunnel-only fault would trigger.
- `kirocrew restart` already has the refusals this needs. It refuses an active or installed-but-stopped service, a unit name that is an alias (`_refuse_service_alias`), a desktop-supervised gateway (`_refuse_desktop_supervised`), a live or indeterminate lock holder (`_refuse_lock_holder`, `_refuse_if_lock_held`), and it waits for the new gateway to answer `/api/ready` before it reports success (`_wait_gateway_ready`).
- The crash dumps are not fenced from agents. `logs/crash-dumps` is in neither `_CREW_SECRET_LEAVES` nor `_WRITE_PROTECTED_HOME_PATHS` (`security/paths_lists.py`), nor in the sandbox's `_CREW_READONLY_LEAVES` (`sandbox_leaves.py`). The store's own comments say so: `_DUMP_READ_MAX_BYTES` notes that "the dump directory is not agent-fenced", and `_read_healthy_marker` hardens its reader for the same reason.

### Problem

A remote gateway with no service manager that crashes stays down until someone notices the panel and logs in to the host. The hub already knows the host is reachable and the dashboard is not answering, and the remote already records whether the last exit was a crash. Nothing connects the two.

## 3. Goals

- With the opt-in on, a remote gateway with no service manager that crashed or stalled is started again once, without a person, within the time self-heal already takes to give up.
- A gateway someone stopped stays stopped. A gateway that is running, even hung, is never stopped or restarted.
- Every case the remote cannot vouch for starts nothing, including a remote too old to know the command.
- Each outcome shows in the tunnel status in words a person can act on.
- With the opt-in off, nothing changes.

## 4. Non-goals

- Restarting a hung gateway. That needs a decision about cutting live work, and the 2026-11-10 ruling covers it.
- Replacing a service manager. Installing the gateway as a service stays the recommended setup.
- Chained, Fargate and headless crews. `_remote_command_target` already refuses every remote verb for them, and this one is no different.
- Telling a deliberate `kill -9` or a double Ctrl-C from a crash (§8.3).
- Fencing the hub's own `instances.json` from agent file tools (§8.2).
- A notification channel. Self-heal adds none today, and this adds none.

## 5. Design

### 5.1 The opt-in

`Instance` in `instances/registry.py` gets one field beside `disabled` and `cloud_stopped`:

```python
start_after_crash: bool = False
```

`Instance.from_dict` reads it as `data.get("start_after_crash", False) is True`, the same strict read `disabled` gets, and `InstancesRegistry.update` adds it to its allowed set. `PATCH /api/instances/{id}` (`api_instances_update` in `dashboard/handlers_instances.py`) adds it to `_PATCH_FIELD_TYPES` as `bool`. Setting it is owner-only: the route applies the same positive owner check `_update_owner_denial` applies to "Update remote", because turning it on lets the hub run a command on another machine without a click. A chained or headless crew is refused with the code `_remote_command_target` gives it. Settings > Remote Crew shows it as a per-crew switch, "Start the remote gateway after a crash", with one line of help: "Only for a gateway with no service manager. Never restarts a running gateway."

`instances.enabled` stays the global switch. A per-crew field is the narrowest grant, and owner intent about one host is already per-record (`disabled`, `cloud_stopped`).

### 5.2 When the hub sends the command

Only from self-heal, never from the manual Diagnose button, and only when all of these hold:

1. `start_after_crash` is on for the crew.
2. `_recover` gave up: either the attempt cap (the `attempts > self._max_recovery` branch) or the one-hour window, whichever comes first. At the defaults the cap comes first, at about six minutes. A crew tuned with a high `max_recovery_attempts` can reach the window first, and both branches already call `_schedule_diagnosis`.
3. The diagnosis self-heal scheduled at that point returned `REMOTE_DOWN`. Any other code (`SSH_UNREACHABLE`, `SSM_ACCESS_DENIED`, `REMOTE_NOT_INSTALLED` and the rest) names a fault a gateway start cannot fix, so nothing is sent.
4. The command has not been sent for this crew in this outage.
5. `_remote_command_target` resolves the crew to `(inst, params)`. A refusal there (`not_found`, `invalid_transport`, `chained`, `headless`) sends nothing, is logged once per outage, and leaves `gateway_start` as it was. It does not count as a send. The PATCH route refuses the flag on chained and headless crews, so this is reached only when a crew was edited after the flag was set.

To do this, both give-up branches call a new `_maybe_start_after_crash` in place of the bare `_schedule_diagnosis` when the flag is on. Under the manager lock, with no `await` between the check and the add, it checks condition 4 and adds the id to the outage latch (below). It then schedules `_diagnose_then_start`, which runs `diagnose` exactly as today, checks conditions 3 and 5, and sends the command. When the diagnosis is not `REMOTE_DOWN`, or condition 5 refuses, the task removes the id again, so a later give-up in the same outage can still try. Two give-ups that overlap can't both send, because the second finds the id already claimed. The task is tracked with `_track_recovery`, runs without the lock like diagnosis, and is cancelled with the rest of the crew's recovery tasks on disconnect or removal.

**The outage latch.** An outage starts at the first failed recovery and ends at a probe that answers end to end (`_on_tunnel_healthy`), a disconnect, a removal, or a hub restart. The manager keeps the latch, `_start_after_crash_sent`, in memory, and clears an id only at those points. So whatever the command returns, including a timeout, it is not sent again in that outage. A disconnect ends the outage because the person then chose to reconnect. A hub restart ends it because the latch is not persisted. In both cases the remote's own checks (§5.3) still decide a later send, and a gateway that crashed again on the first start is still `crashed`, so the worst case is one more start per hub restart. Persisting the latch would mean writing `instances.json` from self-heal for this one bit, which the design does not need.

After a `started` answer the hub replaces any pending give-up retry with an immediate recovery cycle, so the tunnel comes back as soon as the new gateway answers. `_retry_recovery` adds nothing while a recovery task is already queued for the crew, so the implementation cancels the queued `_GIVE_UP_RETRY_SECS` task first. After the one-hour window branch, which schedules no retry, the same immediate cycle resumes self-heal with a fresh window, because a gateway is now up to reconnect to.

The command is a fixed literal, `START_IF_CRASHED_SUBCOMMAND = "start --if-crashed"`, declared in `instances/remote_update.py` beside `RESTART_WHEN_IDLE_SUBCOMMAND` and sent through `_run_remote_subcommand`, whose contract is that the subcommand is always a literal from that module. It passes `timeout_secs` large enough for the remote's ready wait (`_RESTART_READY_TIMEOUT_MAX`, 180 s, plus margin), as `_update_then_restart` does with `REMOTE_UPDATE_TIMEOUT_SECS`, and `stdout_tail=True` so the answer line survives an SSH banner.

### 5.3 The remote command: `kirocrew start --if-crashed`

A new top-level command. `--if-crashed` is required in this version, so `kirocrew start` alone is a usage error and nothing can start a gateway through it without the check. It takes no `--port`: like `kirocrew restart`, it resolves the remote's own port with `resolve_client_port`.

In one process, in this order, it:

1. **Refuses a managed gateway.** If `service_controller.installed_unit_path()` names a unit, or `service_controller.is_service_active()` is true, it answers `managed`, whether the unit is running, stopped or failed after its start limit. A stopped or failed unit means the manager or a person decided, so starting a gateway beside it is the thing `_restart` already refuses. An alias (`_refuse_service_alias`) and a desktop-supervised gateway (`_refuse_desktop_supervised`) answer `managed` too.
2. **Refuses when anything is running.** `lock_holder(config_dir())` naming a live pid, or `platform_compat.find_listening_pids(port)` naming any listener, answers `running`. A `LockProbeError` answers `lock_unknown`. The home's lock decides whether this home's gateway is running, as it does for `_restart`, which calls it "the oracle for is something running". The port lookup only adds a check. When `listening_pid_tool_available()` is false (no `lsof` in the trusted system directories, common in a slim container), the lookup is skipped. A foreign process on the port can then go unseen, but the new gateway can't bind a taken port, so that case ends as `start_failed` and nothing is stopped. Refusing whenever `lsof` is missing would turn the feature off in the containers it is for.
3. **Reads the newest crash dump** with `last_exit_verdict` (§5.4). Only `crashed` and `stalled` continue. Every other verdict is the answer.
4. **Starts the gateway** the way `_restart` does after a crash: `_refuse_if_lock_held` once more, then `_spawn_detached_gateway(port)`, then `_wait_gateway_ready`. A ready gateway answers `started` with its pid. A gateway that died answers `start_failed` with `died`, one that did not become ready answers `start_failed` with `not_ready`, and one restart could not confirm answers `start_failed` with `unconfirmed`.

It never signals a process. A gateway that comes up between the hub's diagnosis and step 2 is seen as `running`. One that comes up between step 2 and the spawn holds the home's lock, so `_refuse_if_lock_held` or the new gateway's own lock refusal ends it, and the answer is `running` or `start_failed`.

Each run writes one SEL event, `operation="gateway_start_if_crashed"`, with the answer as its outcome, as `_restart` does for `gateway_restart`.

**Answer contract.** The last stdout line is a marker, `kirocrew-start-if-crashed` followed by one space and a JSON object, for example `kirocrew-start-if-crashed {"outcome":"not_started","reason":"clean_exit"}`. `outcome` is one of `started`, `not_started` or `start_failed`, and `reason` comes from a closed set. Exit status is 0 only for `started`. Every other answer exits 78, and nothing in the command exits 2, which stays argparse's usage error. The command has one exit point. The helpers it reuses from `_restart` (`_refuse_service_alias`, `_refuse_desktop_supervised`, `_refuse_if_lock_held`) refuse by printing and calling `sys.exit(1)`, so the command calls their probes rather than the refusing wrappers, or catches their `SystemExit`, and turns each into its closed reason. Any other exception, including an `OSError` from listing the dump directory, becomes `start_failed` with reason `error` if the spawn already happened, and `not_started` with reason `error` before it.

The hub maps the answer strictly. Exit 2 reads as `unsupported`. Exit 0 must carry a `started` marker, and exit 78 must carry a `not_started` or `start_failed` marker. Every other exit status (1 from an uncaught crash, 255 from ssh, -1 from the runner's own timeout), every mismatch between status and marker, an unknown outcome or reason, a missing marker and malformed JSON all read as `command_failed`. The parser is shaped like `_read_busy_counts`. A test pins the hub's copy of the marker and reasons to the CLI's own, as the idle gate's constants are pinned.

### 5.4 Reading the newest dump

A new function, `last_exit_verdict()`, in `dashboard/crash_dump_store.py`, built only from what the store already has. It looks at `newest_dump()` and nothing older, because `unexplained_exits` keeps an old crash on its list after a later clean stop, and skips stall dumps because they are not header-only.

| Newest dump | Verdict |
|---|---|
| No dump directory, or no dump in it | `no_record` |
| The head can't be read (`_read_dump_head` raised: a symlink, a FIFO, a permission or I/O error) | `unreadable` |
| The `# PID:` line names another PID domain (`_owner_foreign`) | `foreign_owner` |
| No parseable `# PID:` line, or a PID line with no domain (written before the domain was recorded), so `_owner_alive` returns `None` | `unattributable` |
| `_owner_alive` is `True` | `owner_alive` |
| `<dump>.clean` exists (`os.path.lexists`) | `clean_exit` |
| The header lacks `_CLEAN_EXIT_AWARE_TOKEN` (written by a build that never left the marker) | `predates_marker` |
| Header with the token, header-only, owner dead, no marker | `crashed` |
| Header with the token, thread stacks, owner dead, no marker | `stalled` |

The rows are checked in this order, so `crashed` and `stalled` both require a header that carries the token. A stall dump with a marker is a soft dump that a clean stop followed, so it reads as `clean_exit`. `owner_alive` usually means a recycled PID with no start identity to compare, since step 2 already found no lock holder. It starts nothing. The watchdog's dump-then-exit hands SIGALRM to its default disposition, which ends the process without running the orderly exit (`dashboard/loop_watchdog.py` module docstring), so a stall dump has no marker. `os.path.lexists` treats anything at the marker's name as a marker, so a planted link or file can only stop a start.

### 5.5 What counts as a crash

`crashed` covers every death that skipped the orderly exit: a Python crash, an OOM kill, `kill -9`, a SIGHUP when a foreground gateway's terminal closed (the gateway handles only SIGINT and SIGTERM), and a forced exit after a second signal, whose `_force_exit` path does not call `mark_clean_exit`. A host shutdown that sends SIGTERM runs the orderly exit, so it reads as `clean_exit` and starts nothing. The documented stop is `kirocrew stop`, which never escalates past SIGTERM: `_stop` in `cli_server_stop.py` signals only SIGTERM, and for a gateway that won't exit it prints the `kill -9` command (`_force_stop_command`) for a person to run rather than running it.

### 5.6 Fail-closed matrix

Each row is one combination of remote version, gateway state and newest dump. "Start" is yes in exactly two rows.

| Remote version | Gateway state on the remote | Newest crash dump | Answer | Start? |
|---|---|---|---|---|
| Any | Any | Any | Flag off, chained, Fargate or headless crew, a diagnosis other than `REMOTE_DOWN`, or already sent this outage: no command is sent | No |
| No `start` command (every release before this one) | Any | Any | argparse exits 2: `unsupported` | No |
| No `kirocrew` the runner can find | Any | Any | Runner error: `command_failed` | No |
| New | Service unit installed: active, stopped, or failed after `StartLimitBurst` | Any | `managed` | No |
| New | launchd plist installed | Any | `managed` | No |
| New | Unit name is an alias of another unit | Any | `managed` | No |
| New | Desktop app supervises it | Any | `managed` | No |
| New | Live lock holder (a hung gateway, or one starting) | Any | `running` | No |
| New | Something listens on the port | Any | `running` | No |
| New | Lock probe indeterminate | Any | `lock_unknown` | No |
| New | Nothing running | None | `no_record` | No |
| New | Nothing running | Unreadable | `unreadable` | No |
| New | Nothing running | Owner in another PID domain | `foreign_owner` | No |
| New | Nothing running | No parseable PID line, or no PID domain | `unattributable` | No |
| New | Nothing running | Owner reads alive (recycled PID, no start identity) | `owner_alive` | No |
| New | Nothing running | `.clean` present (`kirocrew stop`, SIGTERM, in-place restart) | `clean_exit` | No |
| New | Nothing running | Written before the marker existed | `predates_marker` | No |
| New | Nothing running | The dump directory can't be listed | `error` | No |
| New | Nothing running | Token in header, header-only, owner dead, no marker | `crashed`, then start | **Yes** |
| New | Nothing running | Token in header, stacks, owner dead, no marker | `stalled`, then start | **Yes** |
| New | A gateway takes the lock after step 2 | Crash or stall | `running` or `start_failed` | No second start |
| New | The new gateway dies or never becomes ready | Crash or stall | `start_failed` | Not again this outage |
| New | The transport times out during the command | Crash or stall | `command_failed` | Not again this outage |

A remote too old to report its last exit fails closed. The CLI's root parser is argparse with one subparser per command (`add_subparsers` in `cli.py`, each command registered through `cli_help.add_command`), and main registers no `start`. On a remote built before this change, `kirocrew start --if-crashed` is an invalid subcommand choice, and argparse exits 2 before any command code runs. Even a build that had some other `start` command would reject the unknown `--if-crashed` flag with the same exit 2, because the CLI parses with `parse_args`, not `parse_known_args`. The runners pass the remote exit status through unchanged (`run_remote_kirocrew` in `instances/token_mint.py` returns the process return code, or -1 on its own timeout). The hub reads exit 2 as `unsupported`, the way `read_gate_answer` reads `_ARGPARSE_USAGE_EXIT` for an idle gate the remote does not know. It never falls back to `restart`. A remote build always has the verdict and the command together, because both ship in one phase (§6), so no remote can know the command without the check. An exit 0 without a readable `started` marker reads as `command_failed`, not as a start, so a future build that answered differently would still not be mistaken for one.

### 5.7 What the tunnel status shows

`TunnelStatus` (`instances/ssh_tunnel_manager_child.py`) gets one optional field, `gateway_start`, serialized by `to_dict` as `{"code", "message", "at"}` or `null`. It stays `null` when nothing was sent, so with the flag off the status is the same as today. `message` is always the hub's own fixed copy for the code. The remote's free text and stdout tail go to the hub's log only, never into the status. `TunnelStatus.state`, `error` and `diagnosis` keep their meaning: the diagnosis still reads `REMOTE_DOWN` while the gateway is down.

| `code` | When | `message` | Tunnel afterwards |
|---|---|---|---|
| (none) | Flag off, or nothing sent | Field is `null`. Only today's diagnosis shows. | `error`, as today |
| `started` | The gateway answered `/api/ready` | "Self-heal started the remote gateway after it crashed or stalled." | An immediate recovery cycle runs, and the tunnel returns to `connected` once a probe answers |
| `not_crashed` | `clean_exit` | "Not started: the remote gateway's last exit was a clean stop. Start it on the remote when you want it back." | `error` |
| `gateway_running` | `running` (a live lock holder or a listener on the port) | "Not started: a gateway is running on the remote but its dashboard isn't answering. It may be hung. Restart it from the remote." | `error` |
| `lock_unknown` | `lock_unknown` | "Not started: the remote couldn't tell whether its gateway is still running." | `error` |
| `exit_unknown` | `no_record`, `unreadable`, `foreign_owner`, `unattributable`, `owner_alive`, `predates_marker` | "Not started: the remote can't tell how its gateway last exited." | `error` |
| `managed` | `managed` | "Not started: a service manager or the desktop app owns this gateway." | `error` |
| `unsupported` | Exit 2 | "Not started: the remote runs a version that can't report its last exit. Update it, or start it on the remote." | `error` |
| `start_failed` | `start_failed`, any reason | "Self-heal tried to start the remote gateway, but it did not come up. See the remote's gateway log." | `error` |
| `command_failed` | Transport error, timeout, an unreadable or mismatched answer, or `not_started` with reason `error` | "Self-heal could not run the start command on the remote." | `error`. After a timeout the gateway may still come up, and the next recovery cycle finds it. |

A pre-dispatch refusal from `_remote_command_target` (§5.2, condition 5) and a give-up in an outage that already sent the command both leave `gateway_start` as it was, so the first answer of the outage stays visible. `at` is the epoch second the hub resolved the outcome, whether from an answer or from a transport failure. `_on_tunnel_healthy` clears every code except `started`, so a "Not started" line never outlives the outage it describes. `started` stays until the next command or a disconnect, so a person can see that self-heal acted. The Settings > Remote Crew row (`website/src/pages/settings/InstancesPanel.tsx`) shows `message` as an inline notice under the diagnosis, and the crew pane (`website/src/components/InstancesViewport.tsx`) shows it after the diagnosis reason. The hub logs each answer once at INFO, and at WARNING for `start_failed` and `command_failed`.

### 5.8 Protecting the evidence

`logs/crash-dumps` becomes write-protected in both of the layers that protect `config.json` today:

- For agent file tools, add `logs/crash-dumps` under both `_CREW_HOME_PREFIXES` to `_WRITE_PROTECTED_HOME_PATHS` in `security/paths_lists.py`, which `is_sensitive_write_path` refuses at the file-edit gate. A directory entry covers the dumps, the `.clean` markers, the healthy-boot marker and any temp file inside it.
- For sandboxed processes, add `logs/crash-dumps` to `_CREW_READONLY_LEAVES` in `sandbox_leaves.py`, so a shell command or a spawned script can read but not create, rename or delete anything there.

The gateway writes and sweeps the directory directly, outside both gates, as it does for `cron-running`, so nothing it does changes.

Both lists already hold multi-segment leaves (`apps/ops-mission-control/data/rotation.yaml` on the tool gate, `run/voice-runtime` on the sandbox's hidden list). `get_dumps_dir` resolves to `config_dir() / "logs" / "crash-dumps"`, which is `~/.kiro/crew/logs/crash-dumps` or the same path under `$KIROCREW_HOME`. The tool gate re-anchors crew leaves at a relocated `KIROCREW_HOME` (`security/paths.py`), and the sandbox plan anchors crew leaves at both default prefixes and at the live `config_dir()` (`sandbox_tiers.py`), so both layers cover the directory the dumps are actually in.

This is write protection, not the read+write floor `cron-running` sits on. The forgery the maintainer raised is planting or deleting a marker. A read can't change the verdict, while a plant, a delete, a rename or a changed mtime can. `cron-running` sits on the read+write floor because nothing legitimate reads a marker through a file tool. Agent file tools read the dumps today, though, and hiding them is a separate privacy question that this RFC leaves where it is. `kirocrew doctor` and `collect_bundle` in `diagnostics.py` open the directory directly, so they are unaffected either way. `security/paths_lists.py` states the rule this follows: "The evidence an automatic state change rests on has to be at least as protected as the state it changes."

The read-only mount covers only `logs/crash-dumps`, nested under a `logs/` that stays writable, like `run/voice-runtime` under `run/`. Inside it a sandboxed process can't create, overwrite, append, unlink, rename in or out, symlink, chmod or change an mtime, so it can't make an old crash dump the newest by touching it.

## 6. Migration plan

**Phase 1: remote side.** `last_exit_verdict`, `kirocrew start --if-crashed` with its answer contract and SEL event, and the two protected-path entries. Nothing calls the command yet.

Exit criteria:
- Each row of the §5.4 table has a test that builds the dump state and asserts the verdict.
- Each "New" row of the §5.6 matrix has a test of the command that asserts its answer, its exit status, that only the two "Yes" rows reach `_spawn_detached_gateway`, and that no row signals a process.
- `kirocrew start` without `--if-crashed` exits 2 and starts nothing.
- `is_sensitive_write_path` refuses a write and a delete under both prefixes' `logs/crash-dumps`, and still allows a read.
- The sandbox plan mounts `logs/crash-dumps` read-only, and a sandboxed `rm` of a `.clean` marker fails, at the default data home and at a `KIROCREW_HOME` override, on each platform the OS sandbox supports.
- On macOS, a host with the launchd plist installed answers `managed` (`installed_unit_path` returns `PLIST_PATH` there).
- An `OSError` from listing the dump directory, and each refusing helper's `SystemExit`, come back as a marker answer, never as a bare exit.
- A link named `loopstall-*.txt` pointing at a crash-shaped file reads `unreadable`, a FIFO there doesn't block, and an oversized dump is not read whole.
- A sandboxed write elsewhere under `logs/` still works, and a sandboxed `touch` of an old dump inside `logs/crash-dumps` fails.

**Phase 2: hub side.** The `start_after_crash` field and its owner-only PATCH, `_diagnose_then_start`, the once-per-outage set, `TunnelStatus.gateway_start`, the Settings switch and notices with their i18n keys, and the `docs/system-specs/modules/instances.md` §4 update.

Exit criteria:
- With the flag off, `_recover` and `TunnelStatus.to_dict` behave and serialize as before (existing self-heal tests pass unchanged, and `gateway_start` is `null`).
- With the flag on and a fake runner, the command is sent once per outage: not again after any answer, again only after `_on_tunnel_healthy` or a disconnect. Two give-ups that overlap send it once.
- A `started` answer cancels the queued give-up retry and runs a recovery cycle at once.
- A diagnosis other than `REMOTE_DOWN` sends nothing. The manual Diagnose route sends nothing.
- Runner exit 2 yields `unsupported`, and no other command is sent.
- Each §5.7 code renders its `message`, and every code but `started` is cleared by `_on_tunnel_healthy`.
- A non-owner PATCH of `start_after_crash` is refused.

The phases can ship in either order. Phase 2 against a remote without Phase 1 is the `unsupported` row.

## 7. Backward compatibility

- An old remote with a new hub answers `unsupported`. Nothing starts, and the status says to update the remote (§5.6).
- With a new remote and an old hub, nothing sends the command. The protected path is the only change a person can notice: agent file tools and sandboxed commands can no longer write under `logs/crash-dumps`.
- `Instance.from_dict` tolerates missing and extra keys, so an older registry loads with the flag off. A hub downgraded after the flag was set drops the key on its next write, so the opt-in turns off rather than staying on.
- `gateway_start` is a new key in the status API. Readers that do not know it ignore it.
- The 2026-11-10 ruling forbids restarting a running gateway. This command checks that nothing is running and never stops anything. A gateway it starts is an ordinary start, so an armed update applies at that boot, as it would after a `kirocrew restart` following a crash.

## 8. Security considerations

### 8.1 An agent on the remote

An agent with file tools or a shell on the remote, running as the same user, is the case the maintainer raised.

- With the §5.8 fence, agent file tools and sandboxed commands cannot plant or delete a marker or a dump.
- Without the OS sandbox (`agent.sandbox: "off"`, or a platform with no backend), a shell command can still delete a `.clean` marker. That is the same residual `config.json` has. What it buys: a gateway someone stopped is started once, only with the flag on, only when nothing is running, and only after self-heal gave up. An agent with an unsandboxed shell can already run `kirocrew gateway` itself, so forging the evidence gives it nothing new. Planting a marker only stops a start, which is what happens today.
- The hub never reads the evidence. It sends a fixed literal and reads back one outcome from a closed set. The dump is parsed only on the remote, in the remote user's own process, by the store's existing header reader. `_read_dump_head` opens with a link refusal (`ELOOP`), non-blocking on POSIX, requires a regular file after the open, and reads at most `_HEADER_SCAN_BYTES` (8 KB). `_dump_owner` and `_is_header_only` both go through it. `_list_dumps` ranks entries by `stat`, which follows a link, so a link named `loopstall-*.txt` can rank newest. The header reader then refuses it, and the verdict is `unreadable`, which starts nothing. A FIFO can't block the open, and an oversized file is never read whole.

### 8.2 The hub side

- Only the owner can turn it on, through the PATCH route's positive owner check (§5.1). An app token or a Slack-minted dashboard subject is refused, as for "Update remote".
- `instances.json` on the hub is not fenced from agent file tools today. It already holds `ssh_host` and `remote_bin`, which choose where the hub connects and which program it runs remotely, and the hub already runs `remote_bin` with no click: every token mint runs it on the remote (`custom_remote_bin` in `instances/token_mint.py`), and self-heal's tier 2 mints unattended (`_recover` calls `_mint_for`). So an agent that can edit the file already has unattended execution of a path of its choosing on the remote. The flag adds a narrower unattended action: one fixed command that checks itself on the remote. Fencing `instances.json` would close the larger existing gap and belongs in its own change.
- No new input reaches a shell. The command string is a module literal, the status text is the hub's own copy, and the remote's stdout tail is redacted and capped by the runner before it reaches the hub's log.

### 8.3 A deliberate hard stop

A `kill -9`, a double Ctrl-C, or closing the terminal of a foreground gateway reads as a crash (§5.5), so with the flag on that gateway is started once. An uncatchable signal can't record intent, and a forced exit skips the orderly path that would. The documented stop is `kirocrew stop`, and the Settings help for the switch says so.

## 9. Testing

- Unit tests for the §5.4 verdicts and the §5.6 rows, as listed in Phase 1's exit criteria, with fake service, lock and listener probes, and a fake spawn.
- A pin test that keeps the hub's marker name and reason set equal to the CLI's.
- Hub tests with a fake `_run_remote_subcommand` for each runner answer, including exit 2, -1 (timeout) and a malformed marker.
- Self-heal tests for once per outage, the `REMOTE_DOWN` gate, and the immediate recovery cycle after `started`.
- Path-gate and sandbox-plan tests for §5.8.
- Frontend tests that each `gateway_start.code` renders its notice in both surfaces.
- One end-to-end check on a throwaway remote with no service manager: kill the gateway with SIGKILL and see it come back once; stop it with `kirocrew stop` and see it stay down with the `not_crashed` notice.

## 10. Alternatives considered

- **Send `restart --when-idle` or bare `restart`.** Both restart a live gateway: the first an idle one, the second any one. A tunnel-only fault would restart a healthy gateway.
- **Have the hub read the dump over SSH and decide.** The hub would parse files an agent on the remote can influence, and the check and the start would be two commands with a race between them. One command decides and acts in one process.
- **A read-only status command plus the start command.** The same race, and one more command to keep compatible. The start command's answer already carries the verdict.
- **A global `instances.*` setting.** The global settings tune timeouts and retry counts. Starting a gateway acts on one host, and intent about one host is per-record.
- **Retry the start with backoff.** A gateway that crashes on boot would loop. The outage latch (§5.2) bounds it to one start per outage.
- **Write the `.clean` marker on a forced exit too.** A forced exit usually follows a teardown that wedged, and the header-only dump is `kirocrew doctor`'s only trace of it. Marking it clean would hide that.
- **A new stop-intent marker written when the first signal arrives.** It would cover the double Ctrl-C, but it is a second record of how the gateway ended beside `.clean`, and every reader would have to agree on which one wins. `kirocrew stop` already records intent.
- **Only tell people to install a service.** Still the recommendation, but a container with no init system, or a host where the user can't install units, has no service to install.

## 11. Open questions

None blocking. The switch's label and the §5.7 copy can change in review without changing the design.
