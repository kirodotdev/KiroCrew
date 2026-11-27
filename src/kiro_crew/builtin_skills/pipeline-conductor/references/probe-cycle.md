# The probe cycle

Moved verbatim from the pipeline-conductor `SKILL.md` core; the core says when to read each part.

## The probe cycle

One `fleet_probe.py` call. Keep `probe-config.json`'s `sessions` list synced
with the ledger's open sessions (add on dispatch, drop on close). Fired lines
carry **metadata only** — the probe never emits transcript text:

```
ALERT <key>  <age>s <TAG> i=<index> d=<digest12>
BANNED pid=<pid> rule=<regex|argv:<shape>> cwd=fleet|unknown age=<secs|?>s scope=suite|paths|unknown cmd=<program,flags,+withheld>
OK <n> watched, <m> fired | load/cpu <x> (ok|hot)|n/a | mem <n>G|n/a | banned <n>|n/a | foreign <n>|n/a | deliver init-timeout <a>, watchdog <b>
```

`n/a` in place of a value means the host could not measure that field.
`banned n/a | foreign n/a` means there is no `/proc` to scan (for example on
Windows), so the banned-ops response cannot fire there; it is never a clean
reading.

A tail with no protocol tag reads as `-` and never fires on its own, and a
protocol word inside a tool card is quoted text rather than a report — so a worker
whose only "status" is in a tool call is silent as far as the probe is concerned,
and ages into `IDLE`.

`cmd=` on a `BANNED` line is the matched command reduced to what cannot hold a
secret: a recognised runner or launcher name, recognised option names with their
values dropped, `+<n>` for the arguments withheld, and a trailing `~` on any single
token long enough to be clipped. NO option value is printed, the worker-count
flag's included — `-n0` prints as `-n`, because a custom rule can point this scan at a
program whose `-n` value is a numeric secret and nothing tells that apart from a
worker count. Recognised means drawn from a fixed list, so a program or long option
the list does not name is counted rather than printed — a word that looks like a
program name is also exactly what an opaque credential looks like. One program
spelling is recognised by SHAPE instead: a versioned pytest alias, which prints as
the fixed label `pytest-<version>` or `py.test-<version>` rather than as itself, so
the version it carried never reaches the line. An inline
`KEY=value` in front of the command is withheld whole. Read it before stopping
anyone — it is what separates a real budget-bypassing run from a command that merely names
one, and no argv is echoed. When the program itself is withheld, `rule=` is what
identifies the command: it is the rule that selected this pid.

`rule=` carries one of two vocabularies. A value that reads as a regex is the
banned-process rule whose match selected the pid. A value prefixed `argv:` names a
shape the probe recognises from the argv tokens instead, because the joined command
line cannot express it: `argv:pytest-runner-uncapped` is a runner spelling that is
also a well-formed filename or path component — a versioned alias (`pytest-3`),
`py.test`, or `pytest.exe` — standing in the program position with an explicit numeric
worker count of two or more among its own arguments (the budget-bypassing form;
"uncapped" is the label's historical name, kept because consumers pin it). There
is no regex to look up for such a row, so `cmd=` is the corroborating field: the runner
name prints there, because an `argv:` row has no rule text to identify it by. An
`argv:` shape is offered whatever the rule list
holds, because rule ORIGIN is what carries built-in authority here — the same basis
the wrapper exemption is written against — so a `banned_process_res` edit cannot
switch it off. What can is the named opt-out `argv_runner_detection: false`, which
disables this shape and nothing else. The two settings are independent: replacing the
rule list leaves the shape on, and disabling the shape leaves the rules in force.

The handled set keeps the last dispositioned PAYLOAD report as `settled`, so a
later `IDLE` or `NOPROGRESS` mark on the same session cannot resurrect a ruling
you already delivered. You do not maintain this — it is written on every mark.

When a ruling needs content, read that one session through the
workspace-authorized session tools. Act, then `--mark-handled KEY TAG DIGEST`
(DIGEST is the `d=` field on the fired line), or the signal re-fires forever. A
stale digest is refused (exit 3): the payload moved on since you read it —
re-probe and act on what is there now, never mark blind. A mark against a state
file that exists but cannot be read is refused (exit 4) and the file is left
untouched, because writing past it would erase every disposition it holds. A
transient lock clears on the next attempt; a file that keeps refusing needs a
person to repair or remove `<config>.state.json`, so escalate it rather than
retrying every cycle.

**Classification anchors at the START of a worker's message, and tolerates
leading markdown.** A worker that writes `**BLOCKED:**` is following the protocol
and must be read as blocked, so the classifier strips leading emphasis, list
markers and blockquote marks before matching. That belongs in the probe rather
than in the brief: a rule the worker has to remember fails exactly under the
pressure that produces escalations, and the message that goes unseen is then the
escalation itself. The brief asks for a bare prefix as well, but as the weaker
half of the pair.

`i=` is an **absolute per-session message counter, counted from the start of the
transcript** — not an offset within the tail window. That distinction is the
whole rule: a window-relative index saturates once a session grows past
`tail_bytes` and then reads as a frozen number, which is exactly the
no-progress deadlock the field exists to detect. Absolute counting costs nothing
extra, because `tail_bytes` bounds how much of the file is PARSED, not how much
is read from disk — the read loads the whole transcript either way. The index is
carried into the handled-set entry as well, so the comparison is available next
cycle without you having to hold it.

**An unchanged index since you last acted is no progress**, whether or not a
turn is open — it is the one discriminator a self-deadlocked worker cannot fake,
because producing a message is the thing it cannot do. The probe makes that
comparison itself and fires `NOPROGRESS`, so read the tag and **never diff two
cycles by eye**: a comparison that lives in this document is enforced by
nothing, so it may simply never happen. `i=` is the corroborating number, not
the test. "Still working" is not something the probe can tell you at all — that
reading comes from `session_read_message`'s running flag, and an open turn is
satisfied by a shell deadlocked on its own child just as well as by real work.

**One-time degradation on the first probe after an upgrade.** The fired-line
digest is keyed on the classified tail text, so any change to what gets
classified rotates the digests. A signal that was already marked handled, and
whose classification moved, therefore re-fires ONCE on the first cycle after the
probe is upgraded. Expect a burst of re-fired signals there and disposition them
against the ledger rather than treating each as new work; it is a consequence of
digest keying, not a defect, and it does not recur.

| Line | Action |
| --- | --- |
| `ERR` | Batch-resume the affected workers with `session_send`, `steer: true`: "resume; re-state your protocol prefix". |
| `PR` | Record PR number + head in the ledger. |
| `GREEN` | Verify independently (below). Pass → digest + mark item `green_verified`, backfill a queued item. Fail → send the worker the delta with `session_send` and `steer: true`. |
| `BLOCKED` | Adjudicate (below). Record it in `open_rulings` and clear that entry only once the ruling is delivered via `session_send` with `steer: true`. |
| `STANDDOWN` / `PROPOSAL` | Verify the evidence is stated; record the disposition; unclaim with an evidence comment; close or re-queue; backfill. |
| `TERMINAL` | The last dispositioned protocol report was terminal and the session has gone quiet since. **Close it out** — confirm the disposition landed, release the claim, `session_close`. Do NOT nudge: a finished worker has nothing to re-arm, and a monitor loop is only correct while something EXTERNAL can still change. |
| `IDLE` | Intervention ladder (below). |
| `NOPROGRESS` | The session has produced nothing — no message, no tool row — since you last acted on it, and that mark is at least one `idle_alert_secs` old. Check the **EFFECT, never liveness**: did the artifact appear, did the remote head move, is there a new commit. Effect present → healthy-slow; extend and name the expected completion signal. Effect absent → **route on the line's own age**, because two paths reach this tag and they do not mean the same thing. Within `idle_alert_secs` the transcript is WARM — held alive by inbound traffic the session never answers — and the first move is **not a nudge**, since a nudge is more of the input that produced the reading: enter the intervention ladder at its **Inspect** step. Past `idle_alert_secs` the session is cold as well as unproductive, so `IDLE`'s ladder applies from the top: the classifier ranks this tag below the clock, but the suppression fallback substitutes it for an already-dispositioned report with no age test, so a cold line can carry it. |
| `GONE` | Transcript missing — treat as reclaim: re-queue the item with evidence. |
| `BANNED pid=…` | Banned-ops response (below), keyed by the line's OWNERSHIP CLASS: every class is recorded, and a stop is reserved for `cwd=fleet`. Never read this as a single actionable-or-not decision. Read `age=` to tell the SAME line apart across cycles: an age that GROWS between cycles is one process you have not managed to stop, while a small age under a re-appearing pid is a fresh violation on a recycled number — a bare pid cannot separate those. `age=?s` means the age is unavailable: the process already exited (the expected reading for a short-lived runner), the pid was recycled between the probe's reads (in which case the whole record is stale and `cwd` also drops to `unknown`), or the platform has no `/proc`/`sysconf` to read it from. It never means the process is new. Read `scope=` to rank two banned lines against each other: `suite` is a whole-suite or whole-directory run and is the one worth interrupting first, `paths` is a run already narrowed to files or selectors, and `unknown` is a match naming no runner the probe can read a target from — never treat `unknown` as `paths`. `scope` orders the queue; it does not gate the response. The stop stays keyed to `cwd=fleet` alone, so a `paths`-scoped fleet line still draws it — `scope` only says which fleet line to reach first. `age` says whether it is the same line as last cycle. |

The `OK` line's `deliver init-timeout <a>, watchdog <b>` counters are the
admission instrument, not fleet trivia — see governance.

**If your probe does not emit these fields** — an older bundled probe, or a
build cut before they landed — they are ABSENT, and absent is `UNKNOWN` rather
than a clean reading, exactly as it is for the preflight:

- No `deliver` counters does NOT mean the fleet is delivering; it means you
  cannot see delivery. Fall back to load and memory AND hold admission below
  `max_in_flight`, because the posture table's `ample` row would otherwise be
  satisfied by the absence of its own instrument.
- No `i=` leaves you no progress test. Diff the EFFECT across cycles instead
  (artifact, remote head, new commit) and say in the ledger that progress is
  unverified.
- No `TERMINAL` tag means a finished worker still ages into `IDLE`. Read the
  last protocol report before nudging, or you will nudge a worker that correctly
  has nothing left to do.

An instrument you cannot read is never a green reading. That is the same rule as
exit 3, applied to the probe. And read the fallbacks as the NORMAL path rather
than an edge case whenever the installed probe predates these fields: on such a
build every cycle takes the degraded branch, so "hold admission below
`max_in_flight`" is the effective default and the delivery-keyed posture table is
aspirational until a probe that emits the counters is installed. Check once which
of the two you are running rather than assuming the table applies.

Never page through worker transcripts yourself, and never pull the whole
fleet's state into context — the probe line is the interface. Quiet cycle:
print nothing beyond the probe's own `OK` line and end the turn with
`nothing_to_do`, because a bare stop after the probe keeps the empty-response
recovery.
