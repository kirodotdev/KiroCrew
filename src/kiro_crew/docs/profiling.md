# Profiling (debug-only)

KiroCrew ships aggregate duration histograms (`kiro_crew.metrics`, off by
default) and a stall detector that dumps thread stacks when the event loop wedges
(`kiro_crew.dashboard.loop_watchdog`). Neither attributes time to call paths.
`kirocrew perf sample` fills that gap: it turns a window of execution into
**folded stacks**, the format speedscope, flamegraph.pl and Perfetto all import.

## It is off by default

Profiling never runs unless you ask for it twice: the debug switch must be set
**and** you must invoke the CLI. There is no always-on instrumentation, no
background sampler at rest, and no HTTP endpoint — the CLI is the only entry
point.

```bash
export KIROCREW_DEBUG=1
```

Without it every `perf` command refuses and explains how to enable it. A value of
`0`, `false`, `no` or `off` also reads as disabled, so `KIROCREW_DEBUG=0` does
not accidentally switch profiling on.

## Profiling a code path (no extra dependency)

`--call` imports `module:callable`, runs it in this process, and samples every
thread while it runs. This works everywhere with no extra install and is the
right tool for "why is this one operation slow".

```bash
KIROCREW_DEBUG=1 kirocrew perf sample \
  --call kiro_crew.history:ConversationLog.list_sessions \
  --interval 0.002 \
  --output /tmp/list-sessions.folded
```

The callable is invoked with no arguments. If it raises, the profile is still
written — the stacks leading to a failure are usually the point.

## Profiling a running gateway (needs py-spy)

With no `--call`, the sampler attaches to another process: `--pid`, or the
running gateway's PID read from `$KIROCREW_HOME/gateway.lock`.

```bash
pip install "kirocrew[perf]"
KIROCREW_DEBUG=1 kirocrew perf sample --seconds 30 --output /tmp/gateway.folded
```

A foreign process cannot be sampled from pure Python, so this path requires
py-spy and is a deliberately optional extra. py-spy is located by probing the
usual install locations first (`/opt/homebrew/bin`, `/usr/local/bin`,
`~/.cargo/bin`, `~/.local/bin`) and only then `PATH` — a process launched by the
desktop app or launchd gets a minimal `PATH` with no shell profile sourced, so a
`PATH`-only lookup reports "not installed" on machines that have it. The same
list lives in `website/electron/pyspy-dump.js`; keep them in sync.

On macOS py-spy also needs elevated privileges, because the OS denies
`task_for_pid` otherwise — the same constraint that led `loop_watchdog` to prefer
an in-process `faulthandler` timer over an external py-spy capture. When py-spy is
missing the command says so and points at `--call` instead of producing nothing.

### It can lose a race with the desktop app's crash capture

The desktop app already uses py-spy for a different purpose: when the liveness
monitor decides the gateway is wedged, `website/electron/pyspy-dump.js` runs
`py-spy dump` on it just before `SIGKILL` so the post-restart crash report carries
the frozen frame.

On Linux, ptrace admits exactly **one** tracer per process, so that capture and an
attach from this command cannot both hold the gateway — one is refused. The wedge
capture takes precedence by design: it is the only record of the freeze and it is
time-bounded and unrepeatable, whereas this command is operator-invoked and can be
re-run. If an attach is refused, the error names both possibilities (an existing
tracer, or missing privileges) rather than assuming it is a permissions problem.

macOS behaves differently — py-spy reads via `task_for_pid` there, where several
readers can hold a task port — so a refusal on macOS points at privileges.

## Profiling the desktop app (`kirocrew desktop metrics`)

The two commands above sample **Python**. They cannot tell you anything about the
Electron shell itself -- if the desktop app is burning CPU in its renderer or
growing a window's working set, a py-spy attach on the gateway shows nothing
unusual.

`kirocrew desktop metrics` covers that half:

```
KIROCREW_DEBUG=1 kirocrew desktop metrics
```

### It reads a recording; it does not query the running app

This is the one structural difference from `perf sample`, and it is worth
understanding before you trust the numbers.

`app.getAppMetrics()` is an Electron-main API, so it can only be called from
inside that process. This CLI is a separate Python process, and starting a second
Electron instance does not let it read the first one's metrics. Getting a live
sample on demand would therefore require the desktop app to listen for a request
-- a new local network surface whose only purpose is debugging.

Rather than add one, the app **records**: when it starts with `KIROCREW_DEBUG`
set, `website/electron/perf-metrics.js` samples its own per-process metrics every
5 seconds into a bounded artifact next to the gateway log, and this command reads
that file.

Consequences to keep in mind:

- The app must have been **started** with `KIROCREW_DEBUG` set. Setting the
  variable in the shell you run the CLI from changes nothing about an app that is
  already running without it -- restart the app.
- You see the retained window (the last 120 samples, about ten minutes), not the
  current instant.
- Because it is a recording, it survives the app becoming unresponsive, and it
  keeps the peak. That is usually what you want: the spike you are chasing has
  normally passed by the time you go looking, so the report names both the latest
  totals and the worst sample in the window.

The artifact is bounded on purpose -- it is written on an interval into the user's
log directory, so an unbounded file would be a slow disk leak in a debug aid. It
is rewritten whole via a temp file and a rename, so a read never catches a
half-written file.

### Where it looks

Electron's own log directory, per platform:

| Platform | Path |
|---|---|
| macOS | `~/Library/Logs/KiroCrew/desktop-metrics.json` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/KiroCrew/logs/desktop-metrics.json` |
| Windows | `%APPDATA%\KiroCrew\logs\desktop-metrics.json` |

Nightly builds install under their own product name (`KiroCrew Nightly`), so they
log to a sibling directory -- `~/Library/Logs/KiroCrew Nightly/` and equivalents.
Both are probed, and if both have recorded, the **newest** artifact wins: with
release and nightly side by side, the build you just reproduced against is the one
that wrote last.

Pass `--path` to read an artifact from somewhere else (one attached to a bug
report, for instance). `--json` emits the raw document; `--top N` changes how many
processes are listed.

### Exit codes

| Code | Meaning |
|---|---|
| 1 | `KIROCREW_DEBUG` not set |
| 3 | No artifact found (usually: the app was not started with the flag) |
| 5 | Artifact unreadable, malformed, or a version this build does not understand |

## Reading the output

Each line is `outermost;...;innermost <sample-count>`, hottest first:

```
run (kiro_crew/cli.py:702);search (kiro_crew/history.py:412) 184
```

Open it at <https://speedscope.app> (local, nothing is uploaded) or pipe it to
`flamegraph.pl`.

The command reports the **effective** sample rate alongside the requested one:

```
Sampled 168 ticks over 0.61s (274/s effective, 0.002s requested).
```

A loaded machine cannot always hit the requested interval. Trust the effective
figure — that is the resolution you actually got.

## What the artifact contains

Frame labels are function names plus the last two path components, so absolute
paths and the home-directory prefix are dropped. This holds for both sampling
strategies: the in-process sampler builds labels that way, and py-spy's own output
is rewritten through the same shortening before it is saved. Output is
additionally run through the standard credential and exfiltration-URL redactors,
and written `0o600`, because a profile describes code paths and file layout from
the user's machine. It is still diagnostic data about a real system — review it
before attaching it to a public issue.

## Options

| Flag | Meaning |
|---|---|
| `--call MODULE:CALLABLE` | Profile that callable in this process (no py-spy needed) |
| `--pid PID` | Attach to this PID (default: the running gateway) |
| `--seconds N` | Attach duration, 1–300 (default 10). Applies to the attach path |
| `--interval S` | Seconds between samples, 0.001–1.0 (default 0.005) |
| `--output PATH` | Where to write the profile (default `./kirocrew-profile.folded`) |

Exit codes: `1` gate off or nothing sampled, `2` bad arguments or an
unresolvable `--call`, `3` py-spy missing, `4` py-spy timed out, `5` py-spy
reported success but wrote nothing readable, `6` the profile could not be written
to `--output`.
