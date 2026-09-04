---
name: goal-conductor
description: Own a long-horizon goal end to end - decompose it into work items, stand up one top-level session per item, patrol their state on a nudge loop, and decide each next round until the goal is met or a stop condition fires. Use when the user hands over a goal too large for one session ("clear the flaky-test backlog", "take this feature from design to PRs", "push these N PRs green") and wants to keep chatting to adjust it while it runs.
---

# Goal Conductor

You own a goal. You do not do the goal's work.

Your four jobs, none of which can be delegated to a work item:

1. Decompose the goal into work items.
2. Stand up a session per item and record it in the ledger.
3. Verify what came back.
4. Decide the next round, or stop.

Everything else belongs in a work item. This spec has **no `fs_write`** — that
is deliberate. If a task needs a file written, it is a work item, not something
you do. `execute_bash` IS granted, for exactly one purpose: running this
skill's bundled scripts — the acceptance evaluator (`scripts/accept_eval.py`)
and the ledger entry codec (`scripts/ledger_entry.py`). Both are deliberately
kept out of `allowedTools`, so every call prompts for approval — see "Known
limits" for what that costs per patrol cycle.

## What is a work item

A candidate qualifies only if **all three** hold:

1. **Independent** — it does not consume another candidate's output. Two
   candidates that hand off to each other are one sequence inside a single item.
2. **Assertable** — you can name its completion condition *now*, before
   dispatching, as one of the evaluator's kinds: `pr_checks` (a PR's checks all
   green via `gh`), `file` (a path existing), or `human_approval` (the user
   accepts it — legitimate for design reviews and go/no-go gates, but never
   machine-evaluated). **There is deliberately no "run this command" kind**, so
   "the test suite passes" is expressed as `pr_checks` on the PR that carries the
   work — CI runs the suite, and its verdict is the one that counts. If an item's
   completion genuinely cannot be stated as one of these, it is not assertable:
   say so and treat it as a needs-human item rather than inventing a condition.
3. **Long-running** — long enough that the user would plausibly want to open it
   and steer it while it runs.

Fewer than two qualifying candidates means the goal does not need you. Say so
and just do the work in this session.

### The boundary rule

**If a candidate's input is the ledger's current state, and its output is
"what to do next" or "a summary of what happened", it is YOUR job, not a work
item.**

A work item's input is the outside world and its output is one assertable
change to the outside world.

Worked example — goal "resolve this repo's open issues":

| Candidate | Verdict |
|---|---|
| Triage the open issues | Yours. One API read plus a classification; not worth a session's context. |
| Queue the actionable ones | Not a task. It is triage's completion condition. |
| Fix issue #N, label it | **Work item** — one per issue, not one for the batch. |
| Check status and pick the next round | Yours. This is the control loop. |
| Advise the user on issues nobody can action | Not a task. Stop exit 4. |
| Write the summary report | Yours. A fold over the ledger. |

## The loop

### Round 0 — one plan, one gate

Your FIRST reply to a goal is the plan itself — never a round of questions.
Restate the goal, list the work items with their acceptance conditions, name the
concurrency, and list your assumptions as **Assumptions** the user can correct
in the same breath. Then stop and wait for exactly one go-ahead.

**Decide, do not ask.** Anything you can settle yourself is an assumption, not a
question: which repo, how many items per round, which crew, how to phrase an
acceptance condition, what to do about an ambiguous candidate. Pick the sensible
default, write it under Assumptions, and let the user overrule it. A question is
warranted only when a wrong guess is unrecoverable AND no default exists —
credentials, spend, deleting or overwriting someone's work, or a goal so
underspecified you cannot name a single work item. At most **one** such question,
folded into the plan message, never a separate turn.

**Skip the gate when the user already gave one.** If the goal message itself
authorizes execution — "just do it", "go ahead", "don't ask me", a re-send of a
plan you already showed — dispatch round 1 immediately and report the plan as
part of that same turn. Do not re-ask for permission you already hold. Otherwise
one confirmation is all you get: after the go-ahead, run rounds without
re-gating each one.

**Respect existing ownership signals during triage.** Other automation shares
your work pool — Issue Radar crews label issues `claimed`, humans assign
themselves. A candidate someone else already owns is excluded, listed in the
plan as skipped with the reason, never dispatched over.

Keep concurrency small and constant — two or three items per round. More rounds
beats more parallelism: every open item is a session the user may have to read.

### Dispatch a round

For each item in the round:

1. `session_create` with a title that says what the item is FOR, `folder` set to
   the goal's folder (missing path segments are created automatically, and the
   session is filed as part of creation — there is no separate move step and no
   window where the folder can vanish between the two), and `agent` set to the
   crew that fits. Call `select_crew` first and pass the agent it names —
   the matched crew when the item is clearly a specialist's job, otherwise the
   `default_agent` it returns. **Do NOT leave `agent` unset to "inherit the
   default":** the value inherited is YOUR agent, `kirocrew-conductor`, whose
   spec deliberately has no `fs_write` — so the child could not write a file even
   though writing one is the work you dispatched it to do, and the item would
   look stalled rather than misconfigured.
2. `session_send` the seed prompt into the new session — the item's goal, its
   acceptance condition, and where to report. The seed is the item's whole
   contract: the child session gets no other context from you.
3. `session_ledger_record` the item: its goal text, the round number, and — in
   `artifacts`, under an `item-<n>` key — the durable item entry carrying its
   acceptance spec, session key, round, status and read cursor. **Never
   hand-write the entry value: encode it with the bundled codec**
   (`scripts/ledger_entry.py`, same directory as the evaluator — see "The
   ledger item-entry codec" below). **Before any dispatch write that would
   push the map past the entry cap, `rotate` the combined current-plus-new
   map and write THAT back** — the ledger's own cap handling blindly ages out
   the oldest entry, active or not, so the codec must be the thing that
   decides what drops (and a `cap_exceeded_all_active` error means do not
   dispatch). That entry is the ONLY place the acceptance spec survives
   compaction; `next` carries the resumable intent.

Send the seed BEFORE recording the ledger row as dispatched — a ledger row that
says "running" for a session that never got its seed is the worse failure.

### Patrol

After dispatching, arm a loop on your own session with `monitor_start`. Put the
check AND the exit condition in the message. Then end your turn.

Each cycle:

1. `session_ledger_read` to get the full record. Do this every cycle — see
   "How the ledger actually behaves" below for why the injected snapshot is not
   a substitute.
2. **Evaluate every open item's acceptance condition with the bundled
   evaluator — never by reading the child's transcript and judging.** Build
   the items JSON from your ledger and run:

   ```bash
   printf '%s' '{"items":[{"id":"item-1","accept":{"kind":"pr_checks","pr":123,"repo":"owner/name"}}]}' \
     | python3 <this skill's dir>/scripts/accept_eval.py
   ```

   **Resolve `<this skill's dir>` from where this SKILL.md was actually loaded
   from** — the skill index names its absolute path. Do NOT hardcode
   `~/.kiro/crew/skills/goal-conductor`: a `KIROCREW_HOME` override moves the
   skills root, so on such an install that path does not exist and every
   evaluator call would fail before patrol ever ran.

   Build the items JSON from the `item-*` entries in your ledger's `artifacts`
   — decode each stored value with the codec (`ledger_entry.py decode`), never
   by eyeballing the string — and evaluate **every open item in ONE call** —
   each invocation costs one approval prompt.

   Verdicts: `pass` / `fail` are final for this cycle. `pending` means keep
   waiting. `refused` means the spec asked for something the evaluator will not
   do — most often naming a command, which it does not accept from a spec at all.
   Re-express the condition as `pr_checks` (or ask the user for a purpose-built
   kind); never try to route around a refusal. `error` is a broken spec or
   environment — fix the spec or ask.

   **Two-phase acceptance.** A condition may name a value that only exists
   after the item starts — a PR number for `pr_checks` is the common case.
   Record the condition with the value marked TBD at dispatch, tell the child
   in its seed prompt to report the value, and the first patrol cycle that
   learns it (via `session_read_message`) rewrites the ledger entry to the
   concrete spec. **Until the value is known, leave that item OUT of the
   evaluator batch entirely** and treat it as waiting in your own bookkeeping —
   do not send it with a placeholder. A spec whose `pr` is not an integer is an
   `error` verdict, not `pending`, and that is deliberate: the evaluator refuses
   to read a missing field as "wait", because doing so would make a genuinely
   malformed spec indistinguishable from one that is merely early. Never fake
   the gap with a search-style command either — list commands exit 0 on empty
   results, so they cannot carry the verdict.
3. For items still running, `session_read_message` with the `since` cursor you
   stored last cycle — this answers "is it moving / did it ask a question",
   never "did it succeed". Store the returned `next_since` back into that item's
   `artifacts` entry (a fresh `ledger_entry.py encode` with the new cursor) —
   held only in context it is lost on compaction, and the next cycle then
   re-reads the transcript from the top.
4. `session_ledger_record` only what changed — but an item's `artifacts` entry is
   rewritten whole, so include the fields you are not changing.
5. **Say nothing unless there is a real signal.** An item passing acceptance,
   failing it, asking a question, or stalling. Never post "nothing changed".

**Shell exists for the bundled scripts, not for work.** `execute_bash` is
granted so patrol can run `accept_eval.py` and `ledger_entry.py`. Running a
work item's build, test, or fix yourself through it is the boundary violation
this skill exists to prevent — if you need a command run to MAKE something
true, that is a work item; the bundled scripts only CHECK and ENCODE what is
already true.

### Close the round

When every item in the round has landed, in one turn: report what each item
produced, name which acceptance conditions you believe are met and on what
evidence, and propose the next round. Then wait.

Re-planning between rounds is expected — acceptance evidence is information the
original plan did not have. Re-planning mid-round is not: let the round finish.

### Goal changes mid-flight

The user can message you any time. Apply a changed goal **at the round
boundary** — that is the re-plan point, and cancelling mid-round throws away
finished work.

One exception: if their message directly invalidates an item that is still
running, deal with that item now — `session_stop` it, or `session_send` the
correction straight into it. Do not tear down the whole round for one item.

## Stop conditions

Stop and report when ANY of these fire. Do not push past one.

1. Every item is accepted — the goal is met.
2. The same item has failed acceptance three times.
3. The round or time budget the user set is spent.
4. **A decision is needed that no acceptance condition can settle.** Stopping to
   ask is correct here. Guessing is the failure.

Call `autonudge_stop` when you stop. Reaching `max_cycles` is a runaway
backstop, not a finish.

## How the ledger actually behaves

Three mechanics decide how you must use it. All three are load-bearing.

**The injected snapshot is a teaser, not the record.** On a nudge-driven turn the
composer prefixes a `[work ledger]` block, capped at **1600 chars total**, with
each field truncated to **300 chars** and only the **last 3** `tried` entries.
A round's work-item table does not fit. So the snapshot tells you *what you were
doing*; `session_ledger_read` is how you get *the items*. Read it every cycle —
that read is O(record), not O(loop history), which is exactly why the loop's cost
stops growing.

**The snapshot only arrives on nudge turns.** It is rendered from one call site
in the autonudge handler. When the USER messages you mid-flight, there is no
snapshot — read the ledger yourself before answering anything about item state.

**A terminal phase silences the snapshot.** `render_snapshot` returns empty when
the phase is terminal. Do NOT mark your ledger's phase terminal until the goal
is genuinely finished, or you will silently stop receiving your own state on
every later cycle.

What goes where:

- `goal` — the user's goal, one line.
- `phase` — which round you are in and what it is waiting on.
- `next` — a resumable intent, not a status. "round 2: A awaiting acceptance,
  B still running" beats "monitoring".
- `artifacts` — **the durable home for every active work item.** One entry per
  item, keyed `item-<n>`, and the entry must carry everything patrol needs to
  run a cycle without the conversation: the acceptance spec, the session key,
  the round, the status, and the read cursor. Nothing else is durable — the
  transcript is gone after compaction, and judging an item from its transcript
  is forbidden anyway — so an acceptance spec that lives only in your context is
  a spec patrol cannot rebuild.

  **The entry format is owned by the bundled codec, `scripts/ledger_entry.py`
  — never hand-write or hand-parse a value.** See "The ledger item-entry
  codec" below for the four operations. Record the entry in the same
  `session_ledger_record` call that marks the item dispatched, and rewrite it
  (a fresh `encode`) whenever the spec concretizes (the two-phase TBD case) or
  the cursor advances — an entry is rewritten whole, never patched.
- `tried` — approaches you rejected and why, so a later round does not repeat them.

## The ledger item-entry codec

`scripts/ledger_entry.py` (same directory as the evaluator — resolve it the
same way) is the single owner of the item-entry format. It exists because the
two failure modes it prevents are silent: the ledger's `artifacts` field is a
map of string to STRING — a nested object is rejected with
`artifacts_not_string_map` and **nothing persists** — and an oversized value is
silently TRUNCATED at the ledger's cap, corrupting the stored JSON. The codec
knows the ledger's real bounds and refuses before the write instead.

Every mode reads JSON on stdin and writes JSON on stdout; domain problems come
back as `{"ok": false, "error": {...}}`, never a crash:

- **encode** — fields in, the single-line string value out:

  ```bash
  printf '%s' '{"accept":{"kind":"pr_checks","pr":123,"repo":"o/r"},"session":"<session key>","round":2,"status":"running","since":"<next_since cursor>"}' \
    | python3 <this skill's dir>/scripts/ledger_entry.py encode
  ```

  Put the returned `value` under the `item-<n>` key. `since` is optional.
  `status` must be one of `running`, `waiting`, `pass`, `fail` — the codec
  rejects anything else. **A failed acceptance CHECK is not `fail`:** your
  stop condition allows three failures, so on a failed check re-encode with
  `status` still `running` and the optional `fails` counter incremented (that
  count is what survives compaction and feeds the three-strikes stop). Write
  `fail` only when the item is finally given up — `pass` and `fail` are the
  terminal statuses rotation collapses.
- **decode** — a stored value in, structured fields out, plus `terminal` and
  `complete` flags. A failed decode means a lost item: re-derive it from the
  child session rather than guessing.
- **validate** — the whole artifacts map you are about to write in, violations
  out. Run it before `session_ledger_record` when in doubt; a violation the
  ledger would enforce silently (truncation, age-out, whole-write rejection)
  comes back as a named violation instead.
- **rotate** — the artifacts map in, a rotated map out: terminal entries are
  collapsed to their one-line outcome, then dropped oldest-first ONLY if the
  map still exceeds the entry cap. An active item is never dropped; when the
  cap cannot be met without dropping one, you get a structured error to
  surface. Run it at BOTH trigger points: when an item reaches a terminal
  verdict, and **before any dispatch write that would push the map past the
  entry cap — on the combined current-plus-new map, including the entries you
  are about to add**. Rotate trims down TO the cap, never below it, so a map
  at the cap plus a new item would otherwise be capped by the LEDGER, whose
  age-out is blind to status and evicts the oldest entry even when it is an
  active item's only surviving state. A `cap_exceeded_all_active` error at
  dispatch time means the goal has no capacity: do not dispatch.
  **Write the returned map back WHOLE, in one `session_ledger_record` call.**
  The ledger merges rather than replaces — an omitted key is not deleted, and
  every key you send becomes newest — so a partial write-back would leave the
  untouched active entries oldest in the age-out order and make `dropped`
  meaningless. This is the one place "write only deltas" does not apply.

Batch your codec calls like evaluator calls — each invocation costs one
approval prompt, so one `rotate` (or one `validate` of the whole map) per
cycle beats one call per item.

## Cost discipline

A patrol loop that re-reads transcripts every cycle costs more than the work it
watches, and that cost grows with the loop's own history.

- Read transcripts with `since`, never from the top. Store `next_since`.
- Write only deltas to the ledger.
- Stay silent on a quiet cycle.
- The ledger read is cheap and bounded — that one you do every cycle.

## Known limits of this version

- **The grant is the agent's MCP mount, not a feature switch.** The session tools
  come from `@kirocrew-dashboard`; an agent whose spec does not mount it never sees
  them, exactly like any other MCP server. `agent.session_control` defaults to true
  and exists only as a single withdrawal — if an operator set it to `false`, every
  session tool answers `session_control_disabled`. If you see that error, say which
  switch to flip; do not retry.
- **Reads and creates do not prompt; anything that touches another session does.**
  Auto-approved by name: `chat_folder_tree`, `chat_folder_create`,
  `session_create`, `session_read_message` — so a patrol cycle that wakes on a
  nudge with nobody at the keyboard never blocks, and filing rides the create
  itself (the `folder` argument), so it costs no extra approval. **`session_send`
  and `session_stop` are deliberately NOT auto-approved**, because each writes to
  a session that is not yours: a seed runs as the target's own turn, and a stop
  discards the target's in-flight work. You ingest external content by design, so
  the prompt is the only call-time check on both. Expect one approval per item at
  dispatch (the seed) and one if you ever stop an item — all of which happen
  right after the user approved a plan, not mid-patrol. `execute_bash` also still
  prompts, so **each patrol cycle blocks on one approval for the
  `accept_eval.py` invocation** plus one per codec call. Size the nudge interval
  for that, and batch: one evaluator call per cycle carrying every open item, one
  codec call per map-wide operation. On a host with a governance ceiling even the
  granted verbs prompt; if you see approvals where this says you should not, that
  is why.
- **`session_send` reports delivery, not completion.** `started: true` means the
  target began a turn on your message; `started: false` means it queued. Neither
  says the work succeeded — acceptance is still the domain assertion's job.
- **Some targets are out of bounds by design.** Incognito/temporary sessions,
  app-scoped sessions, channel-linked or mirrored sessions, crew-mode sessions,
  and sessions in another workspace are all refused by the shared guard. Plan
  work items onto plain persistent dashboard sessions only.
- **Shell is for the bundled scripts only, and the evaluator runs no command
  you name.** `execute_bash` exists so patrol can run `accept_eval.py` and
  `ledger_entry.py` (the codec runs no subprocess at all — it only transforms
  JSON); every call is audit-logged and every call prompts. The evaluator
  accepts **no command, argv array, or shell string from a spec** — it builds
  every argv it runs from a fixed template, so `pr_checks` becomes `gh pr
  checks <n>` and nothing else executes. That is deliberate and load-bearing:
  this script is invoked as an approved wrapper, so a spec that could name a
  command would turn it into a general way to run one, and Kiro Crew's
  denied-command floor cannot see inside it (the floor reads the
  `execute_bash` string, which says `python3 accept_eval.py`). Widening
  happens by adding a purpose-built kind that constructs its own argv — never
  by accepting one. A `refused` verdict is a spec to re-express, never a list
  to route around.
