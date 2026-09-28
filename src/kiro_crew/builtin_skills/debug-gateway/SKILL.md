---
name: debug-gateway
description: Load this before concluding a change did not land, and to read what the kirocrew-debug tools actually answered - which question to ask first, the fields that mean the opposite of how they look, and what a 400 / 403 / 404 / 501 from these tools means.
triggers: fix not landed, fix did not land, gateway_predates_head, not_derivable, unverifiable_path, everything is slow, GIL, gil contention, orphan process, orphaned processes, faulthandler dump, loop stall, what happened at, debug_gateway, debug_refusals, debug_threads, debug_processes, debug_snapshots
---

# Reading the kirocrew-debug tools

Five read-only tools answer five questions about the live gateway. The tool
descriptions say what each one covers, and `docs/reference/debug/README.md` carries
the rest. This skill is the part neither of them has: which question to ask first,
and how to read an answer without drawing the opposite conclusion.

Where the surrounding material lives, all in one reference doc. An installed build
does not carry the repository's docs tree, so read it at
[Debugging a running gateway](https://github.com/kirodotdev/KiroCrew/blob/main/docs/reference/debug/README.md),
or at `docs/reference/debug/README.md` in a checkout:

- symptom table: section "Symptom to tool"
- the sandbox traps, including the empty placeholder mounts: section "Sandbox traps"
- who may take each reading: section "Authorization"
- why the server is opt-in: the note under the five questions

## Ask before you conclude

1. **Before reporting that a change did not work, call `debug_gateway`.** A gateway
   that started before your commit is executing the previous revision, and a daemon
   that outlived the change hands out backends built from the old checkout. Both look
   exactly like a broken fix.
2. **A refusal is `blocked-by-policy`'s job**; `debug_refusals` gives it the class.
3. **Before calling a refused path protected, read that class.** Only
   `sensitive_path_match` means the path is protected. `unverifiable_path` means the
   path was never judged, and the row's own `action` line says what to do.

## Fields that read as the opposite of what they mean

### debug_gateway

`gateway_predates_head` has three readings, not two. `null` is neither true nor
false: the start time comes from procfs, so it is absent off Linux, and HEAD's commit
time needs a git checkout, so it is absent on an installed wheel. Treat `null` as
unmeasured and say so, rather than concluding the gateway is current.

`mcp_gateway_daemon.matches_this_install` false has two meanings, so read `fingerprint`
beside it. A real fingerprint that differs means the daemon is serving backends built
from another checkout, and a directive tool can report success against one.
`unknown (pre-fingerprint build)` means the daemon cannot be compared at all, which is
unmeasured rather than stale. `null` means no daemon was described.
`recorder.available` false means the build carries no diagnostics package, which is
also why three of the five tools cannot answer on it.

Either stale reading is fixed by restarting that component, which is the user's step.
Name which one is stale and hand it over rather than re-testing the change.

### debug_refusals

`self` widens to the sessions you spawned only when your caller class may read past
your own rows. An unattended, app-owned, incognito, temporary, channel-linked or
channel-mirrored caller is narrowed to its own rows instead of being refused, so a
short answer there is the scope, not an absence.

`truncated` true means the answer was cut, and it does not say by which limit.
`returned` and `scanned_bytes` do not settle it either, because they overlap: one live
log segment larger than the scan budget is read whole, so the row cap and the byte cap
can both have fired on the same answer. Asking again with a larger `last` is worth one
try, but the route clamps `last` to its own cap, so once `returned` stops growing you
have every row this tool will give. Treat a refusal you cannot see as unknown rather
than absent.

`by_class` is the histogram worth reading first. A pile of `unverifiable_path` is
resolver contention rather than policy. `live.available` false is expected and is not
a fault.

### debug_threads

`mode=now` ships an `interpretation` string that names the explanation its two numbers
support. Read that string rather than ranking the raw columns yourself. Kernel columns
are `null` off Linux, which is an absence of measurement and not a zero.

`mode=sample` only profiles when profiling is switched on: the sampler is gated on
`KIROCREW_DEBUG=1` in the GATEWAY process's own environment, which a tool call cannot
set. Otherwise the answer is a `refused` field, and that field is the shape for every
refusal here, not only for a second concurrent run. Read its message before anything
else; only the concurrent case is worth waiting out.

`deep=true` never runs py-spy, and on a default install it has no py-spy to name: the
tool ships in the optional `perf` extra, so the answer is `available: false` carrying the
reason. Where py-spy is installed the answer is the py-spy `--gil` argv with
`executed: false`, because that capture is a ptrace attach and the gateway does not spawn
its own tracer, so hand that command to the user to run.

### debug_processes

It answers identity and shape, and no CPU rate: `cpu_pct`, `runq_wait_pct` and
`gil_saturated_hint` always read `null` or false, and the tool description says why.
Take a contention question to `debug_threads` `mode=now`.

`owner` is a label, not a session key. Through this route it reads `gateway:<pid>` or
`runtime:<pid>`, which says which gateway tracks the process or which tracked runtime it
hangs off, so do not report it as the session that owns the process. `kind` depends on
that label in one place: a subagent runtime is the same binary as a chat runtime, so
with no session label to separate them it appears as `chat`.

Nothing here kills anything. Reaping stays with the reaper, so report the pid, its kind
and its `owner` label as it is written.

### debug_snapshots

Start with `events_only=true`: the event rows are the ones that explain a moment, and
the series is what you widen to when they do not. `stats` covers every row in the
window even when the page was cut, so its extremes are true. `truncated` true hands
you a `cursor`.

## Reading a refusal from these tools

| Status | `code` | What it means | Next action |
|---|---|---|---|
| 403 | `forbidden` | Several refusals share this code, so read the message. It covers a caller class that holds no host-wide view, a session the gateway cannot name at all, a request from another component, and a named session outside your scope | Never retry and never look for another route. Only for the host-wide-view refusal do the owner's own tab and `session=self` help; an unnameable session reads nothing, including its own rows, and an out-of-scope session key is refused however it is asked |
| 501 | `diag_unavailable` | The build carries no `kiro_crew.diag`, so threads, processes and snapshots cannot answer. Relayed verbatim so it cannot be mistaken for an empty answer | Report the build. `debug_gateway` and `debug_refusals` still work; do not synthesize the missing reading |
| 404 | `dump_missing` | A dump named by an earlier listing rotated away before this read | Re-list and read a current name |
| 400 | `bad_range` | An argument is malformed: an unparsable `around`, a `radius` like `5x`, a non-integer `last` | Fix the argument and call again |

Three more codes appear. `recorder_off` (422) means the recorder is not running, so
the series has no rows. `unavailable` means nothing answered at all, which is a
transport failure rather than a refusal. `too_large` and `truncated_to_fit` mean the
answer outgrew its size budget, and `truncated_hint` states that budget and the remedy.

## If the tools are not there

The server is opt-in, so absence means it was not granted to this agent: say so, point
at the `@kirocrew-debug` reference in the agent's `tools`, and do not shell around it.
