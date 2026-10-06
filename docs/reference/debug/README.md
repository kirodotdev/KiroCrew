# Debugging a running gateway

The `kirocrew-debug` MCP server answers five questions about a live Kiro Crew
gateway. Each tool is one question plus a time window, answered by the process that
owns the truth. Every tool is read-only, and the set is ratcheted to exactly five
names by `test/test_mcp_debug.py` — a write tool cannot be added without that test
changing.

Grant the set by adding the `mcpServers` entry and a `@kirocrew-debug` reference to
an agent's `tools`. It is opt-in: kiro-cli reads `tools/list` once per session, so a
tool listed in core would spend context in every request of every session forever.

## The five questions

| Question | Tool |
|---|---|
| Which code is actually running? | `debug_gateway` |
| Why was I refused? | `debug_refusals` |
| Is the interpreter contended, and by what? | `debug_threads` |
| What is running on this host, and what leaked? | `debug_processes` |
| What happened around a moment that has passed? | `debug_snapshots` |

## Symptom to tool

| Symptom | Tool | What it tells you |
|---|---|---|
| A fix seems not to have landed | `debug_gateway` | Gateway start time against HEAD's commit time, plus `gateway_predates_head` computed for you |
| A directive tool reports success but the gateway logs `not_derivable` | `debug_gateway` | Whether the MCP gateway daemon runs a different fingerprint; it outlived a code change and is serving old backends |
| "User denied tool execution" on a call nobody cancelled | `debug_refusals` | The real class. `unverifiable_path` means retry; `sensitive_path_match` means stop |
| Calls are refused at random, several per hour | `debug_refusals` | The `by_class` histogram. A pile of `unverifiable_path` is resolver contention, not policy |
| Everything is slow and nothing is obviously wrong | `debug_threads` mode=`now` | Run-queue wait against GIL wait. High run-queue wait is a busy host; low run-queue wait beside high GIL wait is GIL contention |
| One process is holding a lot of memory | `debug_processes` | On Linux, rss, thread states, fds and cwd per node, plus `cpu_pct` and `runq_wait_pct`. The rates are deltas against a baseline the route holds, so they arrive from the second read onward; a first read, or a gap under 1 s or over 300 s, returns them null with the reason in `degraded`. `gil_saturated_hint` is gated on `cpu_pct`. On macOS the snapshot carries pid and ppid only: rss, state, threads, fds, cwd and cpu are null and every non-gateway node is `other`, so read `roster.degraded` before concluding anything |
| Sessions closed but processes are still alive | `debug_processes` `orphan_only=true` | The reaper's own orphan verdict, computed by the same function the reaper uses. Not run on macOS, where `orphans: 0` means unmeasured (`roster.degraded` says so) |
| A hot path needs a profile, not a snapshot | `debug_threads` mode=`sample` `seconds=<n> hz=<n>` | Folded stacks over the window (at most 60 s, one sampling run at a time); `deep=true` returns the py-spy `--gil` command for an operator to run outside the gateway, when py-spy is on PATH; it does not run it |
| The loop stalled and a dump was written | `debug_threads` mode=`dumps` | Lists the watchdog's faulthandler dumps; `read=<name>` returns one, scrubbed |
| A file changed at a timestamp and nobody knows why | `debug_snapshots` `around=<ts> radius=5m` | The recorded series and event rows around that second |

## Sandbox traps

These are the readings that look like facts and are not. An agent that trusts them
reaches confident wrong conclusions, which is most of why this server exists.

**Empty placeholder mounts.** The agent sandbox mounts empty, zero-size
placeholders over Kiro Crew's fenced paths — the crew log, the ledgers, `.env`, the
vault. A shell `ls` there shows 0 entries whatever the host actually holds, and
they share one mount mtime. Emptiness proves nothing. A zero-byte `.env` seen from a
sandbox is not a lost credential. Read such a store through a credentialed route —
these tools, the dashboard, or a pod — never from a shell.

**"User denied tool execution."** On kiro-cli this is the wording for a *policy*
refusal. The user did not cancel anything. `debug_refusals` is what separates the
cases, into five classes: `sensitive_path_match`, `unverifiable_path`,
`denied_rule` (carrying its refusal-diagnostic id), `governance` (a profile
ceiling) and `tool_policy_timeout`.

**A refusal is not a match.** The path gate resolves symlinks before comparing a
path to the protected list, and that resolution has a time budget. When the budget
runs out the call is refused fail-closed *without the path having been judged*. It
is transient, it is not about your spelling, and a different reader or a different
spelling meets the same budget. Wait ~30s and retry the identical call. A pathless
MCP tool such as `monitor_inspect` can be refused this way too, so a failed
inspect call says nothing about a monitor's state. `debug_refusals` classes these as
`unverifiable_path` and marks them `retryable`.

**`/proc/<pid>/environ` is unreadable from the sandbox.** The gateway can read
same-uid environs and your shell cannot, which is why `debug_processes
include_env=true` can answer at all — and why it returns only four allow-listed keys
(`KIROCREW_HOME`, `KIROCREW_POD_ROOT`, `TMPDIR`, `KIROCREW_SCRATCH`) and never the
rest.

## Authorization

Authorization lives in the routes (`dashboard/handlers/debug.py`), not in the
server, because the server is a process the agent's own session starts.

The four host-wide views — `gateway`, `threads`, `processes`, `snapshots` — are for
the **owner at a dashboard tab** and nobody else. They carry cross-session metadata
by construction: another session's title as a process owner, Python frames from a
shared interpreter. This is stricter than the crew-log door, which lets a conductor
read the logs of sessions it dispatched, and deliberately so — a dispatch tree
bounds whose *conversation* you may read, but the host is not inside anyone's
dispatch tree.

`debug_refusals` is the one per-session view and takes the crew-log scope: your own
rows, the rows of any session you spawned at any depth, and anything at all for the
owner's tab.

Unattended (`cron:`, `taskrunner:`), app-scoped, incognito, temporary, and
channel-linked or channel-mirrored callers get no host-wide view — including an
owner's own tab that is mirrored, because a mirrored tab republishes every turn.
The exclusions are about where an answer *lands*, not about how much a session is
trusted.

Redaction: every string leaves through `redact_via_context` and `sel._redact_text`;
command lines are redacted; `.env`, the vault and the trust directories are never
read or reported, and the recorder stats only `config.json` and `config.local.json`
(metadata, never bytes); faulthandler dumps are written by C code and cannot
be redacted at write time, so they stay in the fenced directory and are scrubbed on
read-back. Output is capped at 64 KB with a cursor.

## Diagnostic recorder

The gateway runs a diagnostic recorder that `debug_snapshots` reads back. It is on
by default; `KIROCREW_DIAG_RECORDER=0` (or `false`, `no`, `off`) turns it off.

| Variable | Default | Range |
|---|---|---|
| `KIROCREW_DIAG_SAMPLE_SECS` | 30 | 1 to 3600 seconds between samples |
| `KIROCREW_DIAG_RETAIN_DAYS` | 7 | 1 to 365 days of day files kept |

Rows land in `diag/snapshots-YYYYMMDD.jsonl` under the crew data home, one file per
day. After three consecutive samples that each take over 200 ms, the cadence drops
to one sample every 60 s. Event rows use a closed set of kinds: `gateway_start`,
`gateway_stop`, `loop_stall`, `adaptive_cap_lowered`, `memory_posture_change`,
`config_rewritten`, `threshold_crossed`, `proc_burst` and `orphan_appeared`.

The `diag/` directory is masked inside the agent sandbox in every mode. Read it
through the debug tools, which redact on the way out.

## What these tools do not answer

Live path-gate counters (probes, cache hits, budget timeouts, current TTL) are not
exposed: there is no `path_gate_stats()` accessor for the recorder to register as a
source, so `debug_refusals` classifies budget timeouts after the fact from the
security event log and reports `live.available: false`. That block becomes real when
the accessor exists.

A build that does not carry `kiro_crew.diag` cannot answer `debug_threads`,
`debug_processes` or `debug_snapshots` at all. Those routes answer HTTP 501 with
`{"error": "diag not available in this build", "code": "diag_unavailable"}` and the
tools relay it verbatim, so
"this build cannot answer" stays distinguishable from "the answer is nothing".
`debug_gateway` reports the same absence in its `recorder` block, and it and
`debug_refusals` answer on every build.
