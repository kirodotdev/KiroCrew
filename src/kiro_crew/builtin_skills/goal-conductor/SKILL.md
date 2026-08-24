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
you do. `execute_bash` IS granted, for exactly one purpose: running the bundled
acceptance evaluator (`scripts/accept_eval.py`). It is deliberately kept out of
`allowedTools`, so every call prompts for approval — see "Known limits" for what
that costs per patrol cycle.

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

### Round 0 — agree the plan

Restate the goal, list the work items with their acceptance conditions, and say
how many you will run per round. Wait for the user. Do not dispatch on a plan
they have not seen.

**Respect existing ownership signals during triage.** Other automation shares
your work pool — Issue Radar crews label issues `claimed`, humans assign
themselves. A candidate someone else already owns is excluded, listed in the
plan as skipped with the reason, never dispatched over.

Keep concurrency small and constant — two or three items per round. More rounds
beats more parallelism: every open item is a session the user may have to read.

### Dispatch a round

For each item in the round:

1. `chat_folder_create` once per goal (skip if it exists) so every session for
   this goal lands in one place.
2. `session_create` with a title that says what the item is FOR, and `agent` set
   to the crew that fits. Call `select_crew` first and pass the agent it names —
   the matched crew when the item is clearly a specialist's job, otherwise the
   `default_agent` it returns. **Do NOT leave `agent` unset to "inherit the
   default":** the value inherited is YOUR agent, `kirocrew-conductor`, whose
   spec deliberately has no `fs_write` — so the child could not write a file even
   though writing one is the work you dispatched it to do, and the item would
   look stalled rather than misconfigured.
3. `chat_folder_move_session` to file the new session under the goal's folder.
4. `session_send` the seed prompt into the new session — the item's goal, its
   acceptance condition, and where to report. The seed is the item's whole
   contract: the child session gets no other context from you.
5. `session_ledger_record` the item: its goal text, the round number, and — in
   `artifacts`, under an `item-<n>` key, as a JSON-serialized STRING — the durable
   item entry carrying its acceptance spec, session key, round, status and read
   cursor (see "What goes where" below for the encoding, its string requirement,
   and its bounds). That entry is the ONLY place the acceptance spec survives
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

   Build the items JSON from the `item-*` entries in your ledger's `artifacts`,
   and evaluate **every open item in ONE call** — each invocation costs one
   approval prompt.

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
   `artifacts` entry — held only in context it is lost on compaction, and the
   next cycle then re-reads the transcript from the top.
4. `session_ledger_record` only what changed — but an item's `artifacts` entry is
   rewritten whole, so include the fields you are not changing.
5. **Say nothing unless there is a real signal.** An item passing acceptance,
   failing it, asking a question, or stalling. Never post "nothing changed".

**Shell exists for the evaluator, not for work.** `execute_bash` is granted so
this patrol step can run `accept_eval.py`. Running a work item's build, test,
or fix yourself through it is the boundary violation this skill exists to
prevent — if you need a command run to MAKE something true, that is a work
item; the evaluator only CHECKS what is already true.

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
  item, and the entry must carry everything patrol needs to run a cycle without
  the conversation: the acceptance spec, the session key, the round, the status,
  and the read cursor. Nothing else is durable — the transcript is gone after
  compaction, and judging an item from its transcript is forbidden anyway — so an
  acceptance spec that lives only in your context is a spec patrol cannot rebuild.

  **`artifacts` is a map of string to STRING.** The value must be a
  JSON-*serialized* string, not a nested object: a non-string value is rejected
  outright with `artifacts_not_string_map` (HTTP 400), so an entry sent as a real
  object does not persist at all — which loses exactly the state this entry
  exists to keep. Key it `item-<n>`, and serialize the object into one line:

  ```
  item-1 -> "{\"accept\":{\"kind\":\"pr_checks\",\"pr\":123,\"repo\":\"o/r\"},\"session\":\"<session key>\",\"round\":2,\"status\":\"running\",\"since\":\"<next_since cursor>\"}"
  ```

  Parse it back with a JSON decode when you read the ledger, and treat a value
  that fails to decode as a lost item — re-derive it from the child session
  rather than guessing.

  The field's real bounds make this fit and also bound it: **32 entries, key
  ≤128 chars, value ≤2000 chars** — ample for one item's serialized spec, far too
  small for prose or a transcript excerpt. So keep values to this encoding, and
  **rotate**: when an item reaches a terminal verdict, collapse its entry to a
  one-line outcome (`"{\"status\":\"pass\",\"round\":2}"`) or drop it, so a long
  goal's finished items cannot age an ACTIVE item out of the 32-entry cap. Record
  the entry in the same `session_ledger_record` call that marks the item
  dispatched, and rewrite it whenever the spec concretizes (the two-phase TBD
  case) or the cursor advances.
- `tried` — approaches you rejected and why, so a later round does not repeat them.

## Cost discipline

A patrol loop that re-reads transcripts every cycle costs more than the work it
watches, and that cost grows with the loop's own history.

- Read transcripts with `since`, never from the top. Store `next_since`.
- Write only deltas to the ledger.
- Stay silent on a quiet cycle.
- The ledger read is cheap and bounded — that one you do every cycle.

## Known limits of this version

- **The whole surface sits behind one config switch.** `agent.session_control`
  defaults to OFF and fails closed — every session tool answers
  `session_control_disabled` until the user sets it to `true` in config.json.
  If you see that error, say which switch to flip; do not retry.
- **Every dashboard tool call prompts for approval, and so does every evaluator
  run.** That is deliberate: both `@kirocrew-dashboard` and `execute_bash` are
  withheld from auto-approve so their calls still pass through the tool-call hook
  where the deny floor and governance ceiling apply. The steady-state cost is
  concrete and worth planning for: **each patrol cycle blocks on one approval for
  the `accept_eval.py` invocation**, plus one per session-control call in a
  dispatch round. Patrol is therefore attended-unattended — the loop wakes itself,
  but a cycle that finds nobody at the keyboard waits rather than proceeding. Size
  the nudge interval for that, and prefer one evaluator call per cycle carrying
  every open item over one call per item.
- **`session_send` reports delivery, not completion.** `started: true` means the
  target began a turn on your message; `started: false` means it queued. Neither
  says the work succeeded — acceptance is still the domain assertion's job.
- **Some targets are out of bounds by design.** Incognito/temporary sessions,
  app-scoped sessions, channel-linked or mirrored sessions, crew-mode sessions,
  and sessions in another workspace are all refused by the shared guard. Plan
  work items onto plain persistent dashboard sessions only.
- **Shell is for the evaluator only, and the evaluator runs no command you
  name.** `execute_bash` exists so patrol can run `accept_eval.py`; every call is
  audit-logged and every call prompts. The evaluator accepts **no command,
  argv array, or shell string from a spec** — it builds every argv it runs from a
  fixed template, so `pr_checks` becomes `gh pr checks <n>` and nothing else
  executes. That is deliberate and load-bearing: this script is invoked as an
  approved wrapper, so a spec that could name a command would turn it into a
  general way to run one, and Kiro Crew's denied-command floor cannot see inside
  it (the floor reads the `execute_bash` string, which says `python3
  accept_eval.py`). Widening happens by adding a purpose-built kind that
  constructs its own argv — never by accepting one. A `refused` verdict is a
  spec to re-express, never a list to route around.
