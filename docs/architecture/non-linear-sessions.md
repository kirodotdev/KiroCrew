# Non-linear session types for orchestration

A Kiro Crew session is driven from a chat. You send a message, the agent answers, and
the conversation builds up in order. A session is a single thread, and what you see
of it is mostly the latest few messages. To give a view of the whole effort, we optinally 
show a dashboard beside the chat. It is kept current from the session's work
ledger and crew log, and shows what is running, what is done and what needs you.

**Non-linear sessions** add two additional ways to drive a session. 
Each one is a different surface on the session mechanism Kiro Crew already
has, and each is still one session underneath. You work on an object you can see all
at once, and you can hold several conversations in different places on it. 
Two surfaces ship:

- **Super doc.** A markdown document that holds a whole effort: its description, its
  status and its task list. You start a thread anywhere in it, on a highlighted passage
  or at an empty spot, and that thread is how you drive the work in that spot. Ask on a
  task and the agent picks it up, or in Ultra Mode hands it to a worker that does it in
  its own session. A goal on a section keeps that section's work moving on a schedule.
  As work runs, the document shows it: tasks ticked, workers and their state, branches
  and pull requests, all next to the item they belong to.
- **Super canvas.** A FigJam-style canvas with no imposed structure: sections,
  stickies, shapes, text and connectors. You lay out a plan as sections and the links
  between them, then point at a section and ask for it to be done. In Ultra Mode the
  conductor starts workers for the parts, and can open further docs or canvases for
  pieces big enough to need their own. The canvas then shows each worker's progress on
  the part it is doing, so the layout you drew becomes the map of the running work.

Below, they are called docs and canvases for short, and **surface** means either one.

## Two ways to think of a surface

There are two ways to think of what a surface is for, and they differ in where the work
lands.

- **The target is the surface itself.** "Help me write a doc", "help me lay this out on a
  canvas." The agent helps you as you create it, and the document or canvas is the
  work. This is the obvious use, and people get it at once: an editor with an agent in
  it.
- **The target is somewhere else, and the surface directs it.** "Help me organize a plan
  of work and execute on it." The surface holds the plan, the items and how they relate,
  and the agent uses it as the direction for orchestrating work that lands elsewhere:
  branches, pull requests, tickets. A chat session is often used this way too: its
  messages are how work somewhere else gets asked for and followed, not the point of
  the session.

Both are ways of thinking about the surface, not settings. Every surface can be used
either way.

## What a surface is

A surface is a normal sidebar session with one extra fact: it is bound to a **surface id**.
The binding is a field on the session (`_ChatSlot._doc`, persisted as `doc` in the
session's metadata line, so an idle surface comes back as a surface after a restart). When a
session has it, the chat view draws a pane instead of the transcript and composer.

| Surface (kind) | Pane | Surface id prefix | File content |
|---|---|---|---|
| super doc (`doc`) | `DocPane` | none | markdown, optional front matter (`icon`, `cover`, `goal-every`) |
| super canvas (`canvas`) | `CanvasPane` | `b0a4d` | JSON: `shapes`, `connectors`, `asks`, `goalEvery`, `conductorDefault` |

The id is 32 hex characters minted by `dashboard/doc_files.py`; the prefix is how the
kind is told apart (`doc_kind`). The text lives in one file per surface,
`<data home>/docs/<id>.md`. It is plain, so any editor can open it. Reads return the
text and a **version**, the SHA-256 of the file's bytes. A save (`PUT /api/docs/<id>`)
names the version it read; if the file has moved on, the save is refused with
`409 doc_conflict` and the current text, so a person's edit and a conductor's edit
never silently overwrite each other.

## Reusing the session mechanism

Nothing about a surface's conversation is new infrastructure. A surface is a session, so it
gets all of this unchanged:

| Mechanism | On a surface |
|---|---|
| Transcript | The same saved messages (`user`, `assistant`, `tool`, `nudge`, `permission`). The surface reads its threads out of them. |
| Agent, model, memory | The session's own, picked at creation. |
| Tool approvals | `permission` rows, decided on the surface; the trust picker sits on the surface. |
| Work ledger | The conductor records workers with `work_ledger_record`, binds them, and is woken by `work_report`. |
| Loops | Goals are an ordinary auto-nudge loop on the surface's session. |
| Session tree | A surface opened by another surface records its creator (`created_by`), like any worker. |
| Compaction and resume | The same. The surface's standing instructions are re-injected after a compaction. |

What a surface adds is the pane, the file, the conventions in the messages, and a
standing brief. The rest of this document is those four things.

## Plain and Ultra

Every surface has the same two modes as an ordinary session. They decide who does the
work, not where it lands.

- **Plain.** The surface's agent is `kirocrew-worker`. It does
  the work itself, in its own session: reads, edits files, runs commands, works in a
  git worktree on its own branch. The surface is where its work shows: tasks ticked, worktree, branch, test
  results, pull request. It opens no further sessions.
- **Ultra.** The surface's agent is the conductor
  (`kirocrew-conductor`, Ultra Mode). It breaks work up and starts workers, tracks them on its ledger, and is woken when they
  report. The surface is where you steer the workers and see their state.

A new doc or canvas is Plain. Plain or Ultra is chosen on the surface before it has been
asked anything, and is fixed after that, either way: a worker's history and a
conductor's history are not interchangeable.

The mode decides the brief the session gets (below), and, for a surface opened by another
surface's conductor, the default is Plain.

## Threads

A **thread** is a conversation about one place on a surface. A surface holds many of
them at once, but the session underneath is still one transcript. So threads have two
halves: how a thread is marked on the surface, and how many threads share the one
session.

### On the surface

A thread is anchored where the person started it, and the anchor is stored in the
surface's own file, so it survives reloads and edits.

- **Doc.** The highlighted text is wrapped in hidden markers in the markdown. The id
  in the markers is the thread id:

  ```
  <!-- ask:a9ef6eq6 -->the highlighted text<!-- /ask:a9ef6eq6 -->
  ```

  An ask at an empty spot has empty markers. The editor hides the markers and treats
  each as one character, so they move with the text around them.
- **Canvas.** The canvas file lists each thread with the shapes it is about, as
  `asks: [{ id, shapeIds, at }]`.

The conversation itself never enters the file. On a doc, a small pin marks the
anchor and the thread opens in a side panel beside the document; on a canvas, a card
is pinned where the person pointed. What the agent does to the surface (added text,
added shapes, a section a worker owns, wrapped in `<!-- work:SESSION -->`) does enter
the file, through the reply blocks below.

Goals are threads too, anchored to a heading or a section. They are described under
[Goals](#goals).

### In the session

Every thread's messages go into the same transcript, in the order they happen. A
question on one passage, a goal check, then a question on another passage are three
consecutive turns of one session. What keeps them apart is a tag on each turn the
surface sends, and a rule for reading the transcript back.

```
user       [Doc ask a9ef] what is this?          -> thread a9ef
assistant  It is the pricing plan...             -> thread a9ef
nudge      [Goals] Checking the goal sections     -> goals thread
assistant  [goal g1] Nothing new.                 -> thread goal-g1
user       [Doc ask c42b] who owns this?         -> thread c42b
assistant  The billing worker...                 -> thread c42b
```

#### A turn going in

The surface sends a turn only when the person asks something; editing the document
starts no turn. A doc ask is:

```
[Doc ask a9ef6eq6]
what is this?

---
Highlighted:
"""
Two
"""

[Doc title] "Plan"              <- only when the title changed since the last send

<<<DOC                          <- only when the text changed since the last send
...the document, markers included...
DOC>>>
```

- The first line is the **thread tag**, `[Doc ask <id>]`. Every turn with the same id
  belongs to one thread.
- After `---` comes what the ask is about: `Highlighted:` with the quote, `At the spot
  after: "<line above>"` for an empty marker, `At the start of the document.`, or
  `The goal's section:` for a reply on a goal's pin.
- The title and a copy of the document come along only when they changed since the
  last turn. The change test ignores the ask markers, so a new highlight alone is not
  a change, and an edit rides on the next ask.
- The conventions for reading and answering these turns are not repeated in each
  turn. They are in the session's standing brief (see
  [The system prompt](#the-system-prompt)).

A canvas turn is built the same way around the canvas's JSON summary and the
selected shapes. It still carries its conventions in the turn itself; moving them
into the canvas's standing brief is listed under [Built and next](#built-and-next).

#### A reply coming back

The agent's reply is prose plus fenced blocks:

| Block | Effect |
|---|---|
| prose | The thread's answer, shown in the thread. |
| `doc-append` | Markdown added at the end of the document. |
| `doc-insert after="<exact line>"` | Markdown added after that line. |
| `doc-html after="<exact line>"` | An HTML element, drawn as part of the doc. |
| `canvas-add` | New shapes and connectors, placed by section. |
| `canvas-edit` | Changes to existing shapes, by id. |

The agent only ever adds. It cannot rewrite, move or delete what the person wrote.
Each reply's blocks are applied once, keyed by the reply's message id, so a reload
does not apply them twice. A canvas also runs the guard described under
[Canvas access](#canvas-access).

#### Reading the transcript back into threads

The surface keeps no thread store. On every render it splits the transcript into
threads (`deriveThreads` in `docThreads.ts`):

1. Walk the messages in order. A `user` or `nudge` row opens a turn, and one rule,
   `openedThread`, says which thread: a goal wake opens the goals thread, a
   `[Doc ask <id>]` tag opens thread `<id>`, anything else opens none.
2. An `assistant` row belongs to the thread the last opening row opened. Its prose is
   the answer and its fenced blocks are the writes.
3. A thread is waiting for an answer while its last row is the person's.

So a thread's history and its "No reply yet" and "Thinking" states need no extra
state; they are read from the transcript. The only thing kept outside it is the
unread dot, which compares a thread's reply count with a count the browser remembers.
The session view on every surface lists the raw rows, each labelled with the thread
it is read into by the same `openedThread` rule.

## Goals

A goal is a standing instruction on a part of the surface: keep these tickets' statuses
current, watch this PR. It makes a surface act without being asked.

- **Doc.** A hidden line under a heading: `<!-- goal <id>: text -->`. The goal covers
  the heading's section, down to the next heading of the same or a higher level.
- **Canvas.** A `goal` on a section shape; the goal's id is the section's id.
- **One loop, one rate.** All of a surface's goals are checked by a single loop on the
  surface's conductor, at one rate for the surface: 5, 10, 30 or 60 minutes (default 10),
  kept in the doc's front matter as `goal-every`, or the canvas's `goalEvery`.

### The loop

The gateway arms the loop from the saved file (`dashboard/doc_goal_loop.py`), so goals
run with the surface closed. Every save or open of the file brings the session's loop in
line:

- goals present and no loop: arm one, and run the first check at once;
- message or rate changed: rewrite the loop;
- paused: revive it and check;
- no goals left: delete it.

The loop is **ungated**. A work-ledger watch skips every tick on which no worker
reported, which would skip the goal checks too, so the goal check reads the ledger
itself. Arming it displaces the gateway's default conductor patrol; a loop the
conductor armed itself is left alone and the surface's goals do not run until it is gone.

### The goal messages

A wake is an auto-nudge row (role `nudge`) whose transcript text is a short banner,
`[Goals] Checking the goal sections`, while the model receives the full instruction:
what to read (the surface's absolute file path), how to handle each goal, where to write
results (only inside that goal's section), the status overlay to keep, and the reply
format.

```
[goal gabc1]
Marked #26162 done. #26182 still open, no PR yet.
[goal gxyz2]
Nothing new.
```

One block per goal, opened by a `[goal <id>]` line. When there is nothing to report the
block is exactly `Nothing new.` The surface splits the reply by goal id into per-goal
threads named `goal-<id>`, and shows each as a pin on its heading (or section). The
shared thread `__goals__` holds the wake and any text no block claimed. A reply on a
goal's pin is a normal ask with thread id `goal-<id>`.

The pin's chip shows the last eight checks as bars: tall for a check whose report was
not `Nothing new.`, short for one that was, and how long ago the latest finished.

## The system prompt

The conventions every turn relies on live in one place: the session's standing brief
(`doc_session_brief.py`). It is appended to the `[AGENT SYSTEM PROMPT]` block at
session start and re-injected after a compaction, the two moments the contract block is
built. So the model has the conventions once, and the turns stay small.

The brief has two variants, chosen by the session's mode:

- **Plain (worker):** the thread and reply formats, the write blocks and the HTML-block
  rules, the status overlay, and the rule to do real work itself in a worktree and keep
  the surface current.
- **Ultra (conductor):** the same formats, plus the rule to start workers, record them on
  the ledger, and open other surfaces; it does not do the work itself.

It states, among other things: what each tag means (`[Doc ask <id>]`, `[Doc title]`,
`<<<DOC … DOC>>>`, `[Goals]`); that a turn with no document copy means the last copy
still stands; where to read the file if it has lost the copy; that a reply answers its
thread only.

The brief names the surface only by its id. The contract block must be byte-identical
across a session's renders, so nothing that changes mid-session (the title, the text)
is in it. Those arrive as events in the turns: the `[Doc title]` line is the title as an
event. A session is pinned to its surface at turn admission, under the session's lease, and
the brief reads the pin; the pin also carries the mode, so a surface switched between
modes before its first ask gets the right variant.

## The status overlay

The conductor shows what is going on by drawing on top of the surface. The overlay is one
HTML artifact it owns, slug `doc-overlay-<id>`, replaced whenever the picture changes.
Each top-level element is an item:

- `data-anchor` pins it: to exact text in a doc, or `shape:<id>` on a canvas.
- `data-place` places it: margin, over, corner, top, right, bottom.
- `data-open-session` opens a session when the item is clicked.
- Ready-made classes carry state: `ov-status` with `ov-running`, `ov-waiting`,
  `ov-done`, `ov-blocked`, `ov-failed`; `ov-card`, `ov-pill`, `ov-progress`, `ov-spinner`.
  Anything in progress moves; anything finished or stuck is still.

The surface polls the artifact and draws it. Scripts, event handlers, forms and remote
resources are stripped, so the conductor can lay an item out freely without being able
to run code on the surface.

## Canvas access

A canvas can be closed to the conductor by default (`conductorDefault: closed`), with
sections opened or protected one by one (`conductor: allow | deny`). The nearest
container with a setting decides, walking out from the innermost, then the canvas's own.
The conductor is told what it may change in every turn.

Telling is not the enforcement. Every `canvas-add` and `canvas-edit` is filtered before it
lands (`conductorGuard.ts`): whatever touches a protected shape is dropped and named to
the person. Additions are judged twice, once for where the reply asks for them to go
and again for **where they ended up**, because a shape can be asked for in one place and
placed in another. A section added "in" an open section is placed inside it, side by
side, and the parent grows to hold it.

## Surfaces opening surfaces

A surface's conductor can open another surface with `session_create kind=doc|canvas` and
`content`, the new surface's starting file. The new session is bound to a fresh surface id of
its kind and its file is written at birth (a doc gets a random icon and cover unless its
content has its own front matter). It runs Plain unless an agent is named.

- Only a surface's conductor may. Any other caller opens ordinary sessions.
- A chain of conductor-opened surfaces stops two below the surface a person made: the canvas a
  person made opens a doc (1), that doc opens a canvas (2), and that canvas opens nothing
  more. The depth is counted through each session's recorded creator.
- The parent briefs the child with `session_send`, records it on its ledger, and links
  it from its own surface (a session card on a canvas, a session link in a doc). The child
  reports back with `work_report`.
- A surface that arrives with a goal in its content has its loop armed immediately.

## What the model sees

Everything so far describes what a surface sends and what it expects back. It leaves open
one decision about the **message array**: what the harness is given on each turn.

### Option A: one linear session

The surface's saved messages are the session. Every turn from every thread is appended to
the single session in the order it arrived, and the harness continues that session
(kiro-cli keeps its own session record and resumes it). Threads interleave in the
model's context: an ask on one passage, a goal wake, an ask on another passage, a
worker's report.

- Simple, and exactly the existing mechanism. Prompt caching and native tool state work
  as for any session.
- The context grows with the whole surface's activity, not with the thread being talked
  about. Thread B's answer is shaped by thread A's tokens whether or not they relate.
- Threads are serial. The model works one turn at a time.

This is how a surface works today.

### Option B: a thread ledger and a filtered array

The surface's messages are stored by thread id in a **thread ledger** held by the gateway,
and each turn's message array is **built** from it instead of continued. For an ask on
thread `T` the array is:

1. the system prompt (the standing brief);
2. the surface's current state: the title event and the document copy, which already carry
   the shared picture;
3. the **goal messages**: each goal's latest report, and any wake in flight, so the
   model knows what the standing instructions have found;
4. worker reports bound to this surface's ledger items;
5. every message of thread `T`, both sides;
6. the new turn.

Other threads' messages are left out. The relationships between threads live where the
person put them: in the document or on the canvas, which is in (2).

- The context is focused on the thread being discussed, and does not grow with the
  surface's total activity.
- Threads become independent, so several can be answered in parallel, each as its own
  array. The only shared write target is the surface's file, and the version check
  serializes that.
- Editing history becomes meaningful: the ledger is what the model is given, so a
  corrected message changes what it knows, which a native session record cannot do.
- A thread can be closed, archived or branched by moving its ledger rows.

Costs:

- The harness's native session continuity is gone for these sessions. Tool results and
  reasoning from earlier turns are not carried unless the ledger stores them, and a
  stable prefix for prompt caching is something the build must preserve on purpose:
  system prompt and goal state first, thread tail last.
- Cross-thread knowledge exists only if it is in the document or summarized into it.
  The surface's file becomes the shared memory, so the conventions must push durable
  conclusions into it.
- The array's builder owns correctness. A rule that drops a message the thread needed is
  a bug the person sees as the model forgetting.

The two are not exclusive. A surface can run Option A until a thread grows past a budget,
then build thread arrays for the long ones.

### Why it matters for the surface

The point of a surface is that you manage many things at once. Option A keeps the surface
non-linear and the model linear. Option B makes the model's view match the surface: one
focused conversation per thread, with the surface itself as the shared state. The
conventions in this document (thread tags, goal blocks, the document copy) are the same
under both, which is why they are written down as the contract.

## Where the code is

| Concern | Frontend (`website/src/pages/chat/page/`) | Backend (`src/kiro_crew/`) |
|---|---|---|
| Pane selection | `ChatPage.tsx` switch on `currentSlot.doc` | slot field `_doc`, metadata `doc` |
| File store | `DocPane.tsx` load/save | `dashboard/doc_files.py`, `handlers/doc_files.py` |
| Threads and turns | `docThreads.ts`, `docConductor.ts`, `canvas/canvasTurn.ts` | |
| Goals | `docGoals.ts`, `goalLoop.ts`, `DocGoalChip.tsx` | `dashboard/doc_goal_loop.py` |
| Standing brief | | `doc_session_brief.py`, `context_assembly/agent_spec.py`, `dashboard/chat_turn/turn_admission.py` |
| Overlay | `docOverlay.ts` | artifact `doc-overlay-<id>` |
| Canvas access | `canvas/conductorGuard.ts`, `canvas/canvasModel.ts` | |
| Surfaces opening surfaces | | `dashboard/doc_child.py`, `session_control_parts/session_create.py` |
| Mode and session view | `ConductorSession.tsx` | |

## Built and next

Built: doc and canvas surfaces in both modes; threads, goals, and the overlay for both;
the standing brief for docs; server-armed goals; surfaces opening surfaces with the depth cap;
the canvas guard on where additions land.

Next, as design:

- **The gateway applies a reply's file changes.** Writes are applied once per reply by
  the gateway, with the version check, so a conductor can change a surface whose pane is
  closed. Today the open surface applies them.
- **The brief moves into the canvas session's system prompt,** as it has for docs, so
  its turns also carry only what changed.
- **The mode is enforced on the server,** so a surface cannot move between Plain and Ultra
  after its first ask by any route.
- **The thread ledger and the filtered array** (Option B), starting with the long
  threads.
