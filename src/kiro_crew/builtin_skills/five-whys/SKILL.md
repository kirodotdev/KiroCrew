---
name: five-whys
description: Enter a guided dive-deep research mode — 5 Whys is the default engine (one focused question at a time along a What-it-is -> Example -> Why-not-alternatives -> Benefits -> Costs chain, user-steered branching), every step appended to an event-sourced log the tree and report are folded from, and extensible with plug-in capabilities (web search, recap, ...) that share that log.
triggers: 5 whys, five whys, five whys mode, dive deep on, root-cause dive
---

# 5 Whys — guided dive-deep MODE

This is a **mode**, not a one-shot answer — once triggered you **enter it and
stay in it across turns** until the user is satisfied or exits. Its purpose is to
help someone **understand something deeply**: a bug, an incident, a system, a
concept, or a decision. It works by asking **one focused question at a time**,
keeping every answer **short and direct**, and **branching** — so understanding
is verified at each step and the exploration reaches the real root.

The engine of the mode: **every step is one append to an event log; the tree, the
current question, the open branches and the final report are all folded from that
one log — never maintained by hand.** 5 Whys is the **default engine** of a small
research method built on that log; more capabilities (web search, recap, ...) plug
into the same log — see **Capabilities** below.

## The core loop (this is the whole mode)

Each turn does exactly one of these, then **stops and waits**:

1. **Answer** the current question — short and direct (see disciplines below).
2. **Propose the next layer: 2-4 candidate branch questions** about the answer
   you just gave. Each is a single, direct "why...?" / "how...?" / "what causes...?"
   — the obvious next things to dig into.
3. **Let the user steer.** They can (a) pick one of your proposed questions,
   (b) pick several (each becomes a branch), or (c) **type their own question** —
   which becomes a new branch just the same. Never force your menu; the user's
   own question always wins.
4. **Append the events** for what happened (an `ask`, an `answer`, a `focus`
   move...) to the log **via `scripts/five_whys.py`** (see The mechanical core),
   then repeat.

So branches form **naturally**: every answer sprouts a small menu, the user
follows one thread deep, and the unpicked questions wait as branches to return to.

## The reasoning chain (the spine of every dive)

Peeling a thing apart should follow one basic chain of thought. Let it order the
answers you give and the branch questions you propose, so the exploration always
moves from grasping the thing to judging it:

1. **What it is** — a plain, concrete definition, ideally via an everyday analogy
   (a ledger, a git commit, a phone switchboard). Grasp the thing first.
2. **Example** — one concrete, walked-through instance. Abstract only lands once
   it's touchable.
3. **Why not the alternatives** — contrast with the other plausible designs
   (**not necessarily the naive one** — the serious rival approaches too), and
   what breaks with each. This is where real understanding lives.
4. **Benefits** — what this choice buys you that the alternatives don't.
5. **Costs** — what it costs: complexity, edge cases, failure modes, tax on the
   reader. Nothing is free; naming the price is the honest finish.

Use it as a **default spine, not a rigid script.** A node usually advances one
stage along this chain (its answer sits at one stage; the proposed menu offers
the next stage's questions), but the user can jump stages or open a side branch
anytime — their question always wins (see the core loop). Every `ask` event
carries its `stage` (`what` / `example` / `why-not` / `benefits` / `costs`), so
the folded report reads as a complete chain per thread.

## Two disciplines (keep the thinking honest)

- **One focused question per step.** Atomic, answerable in isolation, no compound
  or multi-part questions. If it needs "and also...", split it into two branches.
- **Short answer, expandable on demand.** Default to **one or two sentences** —
  one idea per step is what makes it learnable and keeps each link verifiable. If
  the user wants more on a node, they say "expand / go deeper" and you elaborate,
  but the *default* stays short so the tree doesn't turn into a lecture.

## Who answers

- **Understanding questions -> you answer** from your knowledge, and cite concrete
  evidence (`file:line`, a doc, a log) when the answer is about *their* code or
  system so it's grounded, not guessed.
- **Facts only the user holds -> ask the user** (what they observed, what they
  intended, a decision they made). Two sentences from them, same cap.
- **Never fabricate.** If you can't answer and can't cite it, say so and ask.

## Side discussions

At any point the user can open a **side discussion** to explore something more
freely than the one-question / short-answer rhythm allows — anchored to the
current node but off to the side.

- **Enter:** the user says "side discussion" / "let's discuss this" (optionally
  naming a topic). Say you're in a side discussion anchored at node `[x]` and
  **pause the main loop** — the two disciplines and the question-menu are
  suspended; talk freely, longer answers and tangents are fine.
- **Record it:** append `discuss` events (anchored to node `[x]`) so the exchange
  is kept even though it lives off-tree.
- **Non-destructive by default:** a side discussion **never silently changes the
  tree** — no `ask`/`prune` events are emitted from it unless the user approves.
- **Offer to fold findings in:** when the discussion yields something worth
  keeping (and again when it ends), tell the user they can (a) **add** a new
  branch — emit an `ask` (+`answer`) event, (b) **remove** a node — emit a `prune`
  event, or (c) keep it as discussion-only. Emit those events **only on their
  explicit say-so.**
- **Exit:** the user says "end discussion" / "resume 5 whys". Append a `discuss`
  summary line, apply any approved `ask`/`prune` events, then **resume the main
  loop exactly where it paused** — the last `focus` event in the log is the cursor.

## The event log (single source of truth)

Model the whole session as an **append-only event log**, one JSON object per line,
at `<project-dir>/five-whys/<slug>-<YYYY-MM-DD>.jsonl` (fall back to the workspace
dir if there is no project). **The log IS the state.** You never rewrite it and
never keep a separate hand-maintained tree — the tree, the current question, the
open branches and the report are all **projections you fold from the log** when
you need them, so they can never drift out of sync.

Every line: `{"ts": <ISO-8601>, "type": <type>, ...}`. The types:

| type | key fields | meaning |
|------|-----------|---------|
| `ask` | `id`, `parent`, `stage`, `q`, `origin`(proposed\|user) | a question node is created; **`parent` records the relationship** — this is how the tree exists without being stored (root's `parent` is `null`) |
| `answer` | `id`, `a`, `source`(ai\|user\|`<cite>`) | the answer to node `id` |
| `focus` | `id` | the cursor: node `id` is now the current question (**latest `focus` wins**) |
| `done` | `id`, `kind`(root\|takeaway), `note` | this thread bottomed out |
| `discuss` | `anchor`, `text` | a side-discussion entry, off-tree, anchored to a node |
| `prune` | `id`, `reason` | remove a node/branch — a **new event**, never an edit to history |

This table is the **built-in core**; capabilities extend it with their own types
(see Capabilities). Because every projection ignores types it doesn't recognize,
adding a type never breaks the fold.

Example:

```
{"ts":"2026-08-16T20:00:00Z","type":"ask","id":"1","parent":null,"stage":"what","q":"What is X?","origin":"proposed"}
{"ts":"2026-08-16T20:00:01Z","type":"focus","id":"1"}
{"ts":"2026-08-16T20:00:30Z","type":"answer","id":"1","a":"X is ...","source":"ai"}
{"ts":"2026-08-16T20:01:00Z","type":"ask","id":"1.1","parent":"1","stage":"example","q":"An example?","origin":"user"}
{"ts":"2026-08-16T20:03:00Z","type":"discuss","anchor":"1.1","text":"tangent about ..."}
{"ts":"2026-08-16T20:05:00Z","type":"done","id":"1.2.1","kind":"root","note":"real root cause: ..."}
```

**Projections (fold the log, don't store them):**
- **Tree** = group every `ask` by `parent` (minus `prune`d ids).
- **Current question** = the `id` of the last `focus` event.
- **Open branches / frontier** = `ask`ed ids with no `answer` and no `done`,
  excluding the current focus and any `prune`d ids.
- **Roots / key takeaways** = the `done` events.
- **Report** = one fold over the log (see close-out).

## The mechanical core (`scripts/five_whys.py`)

**All log reads and writes go through this script — never hand-write or
hand-fold JSONL.** It owns the deterministic mechanics (append + schema
validation, id allocation, referential integrity, and folding to projections),
so the LLM only supplies judgment. Run it with `python3` from the skill dir:

```
python3 scripts/five_whys.py ask     <log> --parent ID|root --stage S --stdin-json < f.json   # f.json={"q":"..."}; prints new id
python3 scripts/five_whys.py answer  <log> --id ID --stdin-json < f.json                      # {"a":"...","source":"..."}
python3 scripts/five_whys.py focus    <log> --id ID
python3 scripts/five_whys.py done     <log> --id ID [--kind root|takeaway] --stdin-json < f.json   # {"note":"..."}
python3 scripts/five_whys.py discuss  <log> --anchor ID --stdin-json < f.json                 # {"text":"..."}
python3 scripts/five_whys.py prune    <log> --id ID --stdin-json < f.json                     # {"reason":"..."}
python3 scripts/five_whys.py event    <log> --stdin-json < f.json                             # f.json = the plugin event object
python3 scripts/five_whys.py view     <log>      # current path root->focus + open-branch count (show this each turn)
python3 scripts/five_whys.py tree     <log> | frontier <log> | report <log> --stdin-json < f.json   # {"title":"..."}
python3 scripts/five_whys.py validate <log>      # schema + integrity gate, exit 0/1
```

The script allocates ids, so you never invent them; `prune` cascades to
descendants; unknown (plugin) event types validate fine and are ignored by the
core folds. `report` prints the finished markdown — close-out is one command.

**Free text never rides the shell command line.** A question, answer, note,
citation, title, or plugin-event JSON can contain `$(...)`, backticks or quotes,
which the shell would execute or mangle. So for every command that carries free
text, write the field(s) to a **unique** temp file with the write tool — a fresh
path per write (e.g. via `mktemp`), never a fixed shared path two concurrent
dives could clobber — as **one JSON object**, and pass `--stdin-json`, feeding it
on stdin: `... ask <log> --parent 1 --stage what --stdin-json < <unique>.json`
where the file is `{"q": "..."}`. All of a command's free text (an answer and its
`source`, or the whole plugin event) travels in that single object — so no free
text is ever a shell argument, and two untrusted fields share one read. Short,
safe values (`--parent`, `--stage`, `--id`, `--kind`, `--origin`, `--anchor`)
stay as ordinary flags.

## Capabilities (plugins)

5 Whys is the **default engine**, but the mode is really a small **research
method** built on the shared event log — new capabilities plug in by *reading
that log* and *appending their own typed events*. Nothing else couples them; the
log is the only contract.

A capability declares four things:
1. **Invocation** — a word the user types (e.g. "search", "recap") or a menu
   entry you offer.
2. **Reads** — the shared log (never a private store), so it starts with full
   context.
3. **Emits** — its own append-only event `type`(s), marked **structural**
   (`ask`/`prune` — changes the tree) or **annotation** (everything else — never
   changes the tree, only enriches a node).
4. **Discipline** — same as the core loop: do one thing, append, stop and wait.

Example capabilities:
- **web search:** when an answer needs external facts, search, then append
  `{type:"search", id, query, sources:[...], summary}` and feed it into an
  `answer` whose `source` cites those URLs — a searched answer is never
  ungrounded. (Annotation.)
- **recap:** fold the log so far into a running summary and append
  `{type:"recap", scope:(node|subtree|all), text}` to re-orient mid-session
  without losing the thread. The close-out report is just a recap over the whole
  log. (Annotation.)
- add your own (`hypothesis`, `evidence`, `define`, ...) the same way — pick a
  `type`, say whether it's structural or annotation, done.

## Procedure

1. **Anchor the starting point**, append its `ask` (`id:"1"`, `parent:null`) and a
   `focus` on it. **Resume, don't fork:** if a `five-whys/<slug>*.jsonl` for this
   topic already exists, continue the newest one (fold it, `focus` where it left
   off) instead of starting a second log.
2. **Give the first answer / orientation, then propose the first menu** of 2-4
   branch questions. Append the `answer`; then **append an `ask` for every
   question in the menu** (each `origin:"proposed"`) so the whole menu parks in
   the `frontier`, and `focus` the one the user picks — or append + `focus` a
   question the user types (`origin:"user"`). Unpicked proposals wait in the
   frontier until pursued later or `prune`d; that is what makes "nothing is lost"
   true. Stop.
3. **Loop the core loop.** Pursue the user's chosen thread **depth-first**; each
   answer produces a fresh menu. When they switch, just `focus` the parked node —
   the frontier is whatever the fold says is still open, so nothing is lost.
4. **Bottom out a thread** with a `done` event when the answer is a real root /
   fundamental takeaway (incident: actionable and within the system's control;
   concept: a first principle the user is satisfied with).
5. **Show only the current path each turn** — fold root -> current node, indented —
   with the short answer, the proposed menu, and a one-line "other open
   branches: N". Do **not** paste the whole tree; it lives in the log. Offer an
   `<mcwidget>` tree view (folded from the log) if the user wants the big picture.
6. **Close out -> report.** When the user exits or every branch is `done`, run
   `python3 scripts/five_whys.py report <log>` — it folds the log into a clean
   markdown report (Starting point; the tree with each node's stage -> question ->
   answer, so every thread reads as a What -> ... -> Costs chain; roots / key
   takeaways; side-discussion summaries). Add (for an incident) corrective +
   preventive actions, write it beside the log, and offer to save it as an
   artifact. Because the log is complete, this is pure projection — no
   reconstruction from memory.

## Guardrails

- **Append-only — never rewrite the log.** Correcting or removing something is a
  *new* event (`prune`, a fresh `answer`, a new `focus`), never an edit to a past
  line. History stays intact; that is what makes the fold trustworthy.
- **Never hand-write or hand-fold JSONL.** Every read and write goes through
  `scripts/five_whys.py` — it validates, allocates ids, and folds deterministically,
  so the tree and report can't drift from the log.
- **Never interpolate free text or plugin JSON into the shell command.** Put all
  of a command's free text in one `--stdin-json` JSON object, written to a
  **unique** temp file with the write tool and fed on stdin (see The mechanical
  core); a fixed path risks a concurrent-session clobber, and text with `$(...)`,
  backticks or quotes would otherwise be executed or corrupt the command.
- **Don't dump — drip.** Resist answering the whole topic in one turn; the value
  is the one-step-at-a-time ladder. Short answer + a menu, then wait.
- **The user's own question always branches** — never redirect it back to your menu.
- **"Human error" is never a root cause** (incident mode): the next question is
  why the system *allowed* it, until the answer is a fixable system property.
- **Cause, not blame.** The tree explains mechanism, not who to fault.
- **Don't leap to the root and back-fill** — each level is earned by the one above.

## Interaction mechanics (this runtime)

- One answer + one question-menu, then **stop and wait** — this is what makes it
  a mode, not a monologue.
- Render the proposed menu as a final `[OPTIONS: ...]` line (each option is one
  branch question, phrased in the user's voice) so they click to go deeper — and
  remind them they can just type their own question instead, or say "side
  discussion" to open one (and "end discussion" to come back).
- Keep every turn to: current path + the short answer + the menu + open-branch
  count. The log holds everything; the chat holds the focus.
