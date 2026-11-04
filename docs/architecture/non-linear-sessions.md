# Non-linear session types for orchestration

A chat is a line. You send a message, the answer comes back, and the only part of
the conversation you can see is the tail. That is the right shape for one question at
a time. It is a harder shape for working on an idea: you want to see the whole effort
at once, talk about several things in parallel, and talk about how they relate.

If you have many tasks to get a handle on and spin up work for, one linear thread
puts them through a single file of messages. The overview and the relationships
between the tasks are never in the thread itself.

The product already eases this. A Crew Mode chat pairs its thread with a dashboard in
the side panel, kept current from the session's work ledger and crew log as the work
moves: what is running, what is done, what needs you, with the open questions and
approvals beside it. So the big picture is visible.
What the dashboard does not change is how you talk to the work: it is a readout, and
every question, correction and new piece of work still goes through the one thread,
one message at a time.

A **non-linear session** goes one step further. It is a different surface on the
session mechanism Kiro Crew already has, and it is still one session underneath. What
changes is that you talk to it by working on an object you can look at all at once,
not by appending to a transcript. Two surfaces ship:

- **Super doc.** A markdown document. You see all of it, not a tail. You start threads
  anywhere in it, on a highlighted passage or at an empty spot, so you can discuss the
  high-level description or status of the whole effort and, in the same document,
  individual items in a task list. The document is the entire effort; the threads
  hang off it.
- **Super canvas.** A FigJam-style whiteboard with no imposed structure: sections,
  stickies, shapes, text and connectors. You diagram, brainstorm, plan or run a retro
  on it, and the agent draws on it with you. FigJam showed that one simple surface
  covers a great deal: brainstorming, team retros, planning a trip to a new city.
  Exposing that as a way to drive a session is the bet.

Both are non-linear surfaces on a session. Below, they are called docs and canvases
for short, and **page** means either one. The code calls a canvas a **board** (its
kind, its pane and its reply blocks), so those names appear where code is quoted.

## What the work targets

The question that separates the two uses of a page is where the work lands.

- **The target is the page itself.** "Help me write a doc", "help me lay this out on a
  canvas." The agent helps you as you create it, and the document or canvas is the
  work. This is the obvious use, and people get it at once: an editor with an agent in
  it.
- **The target is somewhere else, and the page directs it.** "Help me organize a plan
  of work and execute on it." The page holds the plan, the items and how they relate,
  and the agent uses it as the direction for orchestrating work that lands elsewhere:
  branches, pull requests, tickets. This is what a chat thread does today. Nobody
  takes a session's message array to be the point of the session; it is how work
  somewhere else gets asked for and followed. A page does the same job, as a surface
  you can see whole instead of a line you see the tail of. This is the less obvious
  use, and the more interesting one.

The two modes below follow the two targets: Plain is one agent, and fits the page as
the target; Crew is a conductor with workers, and fits work that lands elsewhere. The
line is not strict. A Plain agent can still do a single piece of work elsewhere (a fix
in a worktree, written up on its page), and a Crew page's plan is itself edited as the
work moves. But the question a mode answers is the same: is the page the work, or the
way work elsewhere is run?

## What a page is

A page is a normal sidebar session with one extra fact: it is bound to a **doc id**.
The binding is a field on the session (`_ChatSlot._doc`, persisted as `doc` in the
session's metadata line, so an idle page comes back as a page after a restart). When a
session has it, the chat page draws a pane instead of the transcript and composer.

| Surface (kind) | Pane | Doc id prefix | File content |
|---|---|---|---|
| super doc (`doc`) | `DocPane` | none | markdown, optional front matter (`icon`, `cover`, `goal-every`) |
| super canvas (`board`) | `BoardPane` | `b0a4d` | JSON: `shapes`, `connectors`, `asks`, `goalEvery`, `conductorDefault` |

The id is 32 hex characters minted by `dashboard/doc_files.py`; the prefix is how the
kind is told apart (`doc_kind`). The text lives in one file per page,
`<data home>/docs/<id>.md`. It is plain, so any editor can open it. Reads return the
text and a **version**, the SHA-256 of the file's bytes. A save (`PUT /api/docs/<id>`)
names the version it read; if the file has moved on, the save is refused with
`409 doc_conflict` and the current text, so a person's edit and a conductor's edit
never silently overwrite each other.

## Reusing the session mechanism

Nothing about a page's conversation is new infrastructure. A page is a session, so it
gets all of this unchanged:

| Mechanism | On a page |
|---|---|
| Transcript | The same saved messages (`user`, `assistant`, `tool`, `nudge`, `permission`). The page reads its threads out of them. |
| Agent, model, memory | The session's own, picked at creation. |
| Tool approvals | `permission` rows, decided on the page; the trust picker sits on the page. |
| Work ledger | The conductor records workers with `work_ledger_record`, binds them, and is woken by `work_report`. |
| Loops | Goals are an ordinary auto-nudge loop on the page's session. |
| Session tree | A page opened by another page records its creator (`created_by`), like any worker. |
| Compaction and resume | The same. The page's standing instructions are re-injected after a compaction. |

What a page adds is the pane, the file, the conventions in the messages, and a
standing brief. The rest of this document is those four things.

## Plain and Crew

Every page has the same two modes as an ordinary session, and they follow
[what the work targets](#what-the-work-targets).

- **Plain (the target is the page).** The page's agent is `kirocrew-worker`. It does
  the work itself, in its own session: reads, edits files, runs commands, works in a
  git worktree on its own branch. The page is where its work shows: tasks ticked, worktree, branch, test
  results, pull request. It opens no further sessions.
- **Crew (the target is elsewhere).** The page's agent is the conductor
  (`kirocrew-conductor`, Crew Mode). It breaks work up and starts workers, tracks them on its ledger, and is woken when they
  report. The page is where you steer the crew and see its state.

A new doc or canvas is Plain. Plain or Crew is chosen on the page before it has been
asked anything, and is fixed after that, either way: a worker's history and a
conductor's history are not interchangeable.

The mode decides the brief the session gets (below), and, for a page opened by another
page's conductor, the default is Plain.

## The thread

A **thread** is a conversation anchored to a place on the page. It is the unit that
replaces a chat message list.

### Where a thread lives

- **Doc.** A highlight is wrapped in hidden markers in the markdown:

  ```
  <!-- ask:a9ef6eq6 -->the highlighted text<!-- /ask:a9ef6eq6 -->
  ```

  The id is the thread id. The markers travel with the text, survive reloads and
  edits, and are hidden and atomic in the editor. An ask at an empty spot has empty
  markers and no highlighted text. Thread text never enters the document: the
  conductor's answer sits in a collapsed block under the highlight, and only a click
  opens it. A section a worker owns is wrapped in `<!-- work:SESSION -->`, and the
  workers an answer started are listed live in that answer.
- **Canvas.** An ask names the shapes it is about. The canvas file keeps them as
  `asks: [{ id, shapeIds, at }]`, and the answer is a small card pinned where the
  person pointed, never on the canvas itself.
- **Goals** have threads of their own (below).

### What goes to the conductor

Each turn a doc sends carries only what changed. The conventions are not in the turn;
they are in the session's standing brief (see [The system prompt](#the-system-prompt)).
A doc ask is:

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

- The first line is the **thread tag**: `[Doc ask <id>]`. Every turn with the same id
  is one thread, and the reply belongs to it.
- After `---` comes what the request is about: `Highlighted:` with the quote,
  `At the spot after: "<line above>"` for an empty marker, `At the start of the
  document.`, or `The goal's section:` for a reply on a goal's pin.
- The title and a copy of the document ride along **only when they changed** since the
  last turn the page sent. The change test ignores the ask markers, so a new highlight
  alone is not a change. Editing the document starts no turn: an edit rides on the
  next ask. This is what keeps a busy page from firing model calls on every keystroke.
- A canvas turn is built around the canvas's JSON summary and the selected shapes. It
  still carries its conventions in the turn itself; moving them into the canvas's
  standing brief, as docs have, is listed under [Built and next](#built-and-next).

### What comes back

The conductor's reply is prose plus fenced blocks:

| Block | Effect |
|---|---|
| prose | Becomes the thread's answer. |
| `doc-append` | Markdown added at the end of the document. |
| `doc-insert after="<exact line>"` | Markdown added after that line. |
| `doc-html after="<exact line>"` | An HTML element, drawn as part of the page. |
| `board-add` | New shapes and connectors, placed by section. |
| `board-edit` | Changes to existing shapes, by id. |

A conductor only ever adds. It cannot rewrite, move or delete what the person wrote.
Each reply's blocks are applied once, keyed by the reply's message id, so a reload
does not apply them twice. A canvas adds the guard described under
[Access](#canvas-access).

### How the page reads its threads

The page has no thread store. It derives threads from the session's saved messages on
every render (`deriveThreads` in `docThreads.ts`):

1. Walk the messages in order. A `user` or `nudge` row **opens a turn**; which thread
   it opens is one rule, `openedThread`: a goal wake opens the goals thread, a
   `[Doc ask <id>]` header opens thread `<id>`, anything else opens none.
2. An `assistant` row attaches to the thread the last opening row opened. Its prose is
   the answer; its fenced blocks are the pending writes.
3. A thread is **awaiting** while its last row is the person's.

This is also why the page can show a thread's history and its "No reply yet" and
"Thinking" states with no extra state: they are functions of the transcript. The only
thing kept outside it is the unread dot, which compares a thread's reply count with a
count the browser remembers. The
conductor session view on every page lists the raw rows, each labelled with the thread
it is read into, using the same `openedThread` rule, so what the view labels is what
the page does.

## Goals

A goal is a standing instruction on a part of the page: keep these tickets' statuses
current, watch this PR. It makes a page act without being asked.

- **Doc.** A hidden line under a heading: `<!-- goal <id>: text -->`. The goal covers
  the heading's section, down to the next heading of the same or a higher level.
- **Canvas.** A `goal` on a section shape; the goal's id is the section's id.
- **One loop, one rate.** All of a page's goals are checked by a single loop on the
  page's conductor, at one rate for the page: 5, 10, 30 or 60 minutes (default 10),
  kept in the doc's front matter as `goal-every`, or the canvas's `goalEvery`.

### The loop

The gateway arms the loop from the saved file (`dashboard/doc_goal_loop.py`), so goals
run with the page closed. Every save or open of the file brings the session's loop in
line:

- goals present and no loop: arm one, and run the first check at once;
- message or rate changed: rewrite the loop;
- paused: revive it and check;
- no goals left: delete it.

The loop is **ungated**. A work-ledger watch skips every tick on which no worker
reported, which would skip the goal checks too, so the goal check reads the ledger
itself. Arming it displaces the gateway's default conductor patrol; a loop the
conductor armed itself is left alone and the page's goals do not run until it is gone.

### The goal messages

A wake is an auto-nudge row (role `nudge`) whose transcript text is a short banner,
`[Goals] Checking the goal sections`, while the model receives the full instruction:
what to read (the page's absolute file path), how to handle each goal, where to write
results (only inside that goal's section), the status overlay to keep, and the reply
format.

```
[goal gabc1]
Marked #26162 done. #26182 still open, no PR yet.
[goal gxyz2]
Nothing new.
```

One block per goal, opened by a `[goal <id>]` line. When there is nothing to report the
block is exactly `Nothing new.` The page splits the reply by goal id into per-goal
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
  the page current.
- **Crew (conductor):** the same formats, plus the rule to start workers, record them on
  the ledger, and open other pages; it does not do the work itself.

It states, among other things: what each tag means (`[Doc ask <id>]`, `[Doc title]`,
`<<<DOC … DOC>>>`, `[Goals]`); that a turn with no document copy means the last copy
still stands; where to read the file if it has lost the copy; that a reply answers its
thread only.

The brief names the page only by its id. The contract block must be byte-identical
across a session's renders, so nothing that changes mid-session (the title, the text)
is in it. Those arrive as events in the turns: the `[Doc title]` line is the title as an
event. A session is pinned to its page at turn admission, under the session's lease, and
the brief reads the pin; the pin also carries the mode, so a page switched between
modes before its first ask gets the right variant.

## The status overlay

The conductor shows what is going on by drawing on top of the page. The overlay is one
HTML artifact it owns, slug `doc-overlay-<id>`, replaced whenever the picture changes.
Each top-level element is an item:

- `data-anchor` pins it: to exact text in a doc, or `shape:<id>` on a canvas.
- `data-place` places it: margin, over, corner, top, right, bottom.
- `data-open-session` opens a session when the item is clicked.
- Ready-made classes carry state: `ov-status` with `ov-running`, `ov-waiting`,
  `ov-done`, `ov-blocked`, `ov-failed`; `ov-card`, `ov-pill`, `ov-progress`, `ov-spinner`.
  Anything in progress moves; anything finished or stuck is still.

The page polls the artifact and draws it. Scripts, event handlers, forms and remote
resources are stripped, so the conductor can lay an item out freely without being able
to run code on the page.

## Canvas access

A canvas can be closed to the conductor by default (`conductorDefault: closed`), with
sections opened or protected one by one (`conductor: allow | deny`). The nearest
container with a setting decides, walking out from the innermost, then the canvas's own.
The conductor is told what it may change in every turn.

Telling is not the enforcement. Every `board-add` and `board-edit` is filtered before it
lands (`conductorGuard.ts`): whatever touches a protected shape is dropped and named to
the person. Additions are judged twice, once for where the reply asks for them to go
and again for **where they ended up**, because a shape can be asked for in one place and
placed in another. A section added "in" an open section is placed inside it, side by
side, and the parent grows to hold it.

## Pages opening pages

A page's conductor can open another page with `session_create kind=doc|board` and
`content`, the new page's starting file. The new session is bound to a fresh doc id of
its kind and its file is written at birth (a doc gets a random icon and cover unless its
content has its own front matter). It runs Plain unless an agent is named.

- Only a page's conductor may. Any other caller opens ordinary sessions.
- A chain of conductor-opened pages stops two below the page a person made: the canvas a
  person made opens a doc (1), that doc opens a canvas (2), and that canvas opens nothing
  more. The depth is counted through each session's recorded creator.
- The parent briefs the child with `session_send`, records it on its ledger, and links
  it from its own page (a session card on a canvas, a session link in a doc). The child
  reports back with `work_report`.
- A page that arrives with a goal in its content has its loop armed immediately.

## What the model sees

Everything so far describes what a page sends and what it expects back. It leaves open
one decision about the **message array**: what the harness is given on each turn.

### Option A: one linear session

The page's saved messages are the session. Every turn from every thread is appended to
the single session in the order it arrived, and the harness continues that session
(kiro-cli keeps its own session record and resumes it). Threads interleave in the
model's context: an ask on one passage, a goal wake, an ask on another passage, a
worker's report.

- Simple, and exactly the existing mechanism. Prompt caching and native tool state work
  as for any session.
- The context grows with the whole page's activity, not with the thread being talked
  about. Thread B's answer is shaped by thread A's tokens whether or not they relate.
- Threads are serial. The model works one turn at a time.

This is how a page works today.

### Option B: a thread ledger and a filtered array

The page's messages are stored by thread id in a **thread ledger** held by the gateway,
and each turn's message array is **built** from it instead of continued. For an ask on
thread `T` the array is:

1. the system prompt (the standing brief);
2. the page's current state: the title event and the document copy, which already carry
   the shared picture;
3. the **goal messages**: each goal's latest report, and any wake in flight, so the
   model knows what the standing instructions have found;
4. worker reports bound to this page's ledger items;
5. every message of thread `T`, both sides;
6. the new turn.

Other threads' messages are left out. The relationships between threads live where the
person put them: in the document or on the canvas, which is in (2).

- The context is focused on the thread being discussed, and does not grow with the
  page's total activity.
- Threads become independent, so several can be answered in parallel, each as its own
  array. The only shared write target is the page's file, and the version check
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
  The page's file becomes the shared memory, so the conventions must push durable
  conclusions into it.
- The array's builder owns correctness. A rule that drops a message the thread needed is
  a bug the person sees as the model forgetting.

The two are not exclusive. A page can run Option A until a thread grows past a budget,
then build thread arrays for the long ones.

### Why it matters for the surface

The point of a page is that you manage many things at once. Option A keeps the surface
non-linear and the model linear. Option B makes the model's view match the surface: one
focused conversation per thread, with the page itself as the shared state. The
conventions in this document (thread tags, goal blocks, the document copy) are the same
under both, which is why they are written down as the contract.

## Where the code is

| Concern | Frontend (`website/src/pages/chat/page/`) | Backend (`src/kiro_crew/`) |
|---|---|---|
| Pane selection | `ChatPage.tsx` switch on `currentSlot.doc` | slot field `_doc`, metadata `doc` |
| File store | `DocPane.tsx` load/save | `dashboard/doc_files.py`, `handlers/doc_files.py` |
| Threads and turns | `docThreads.ts`, `docConductor.ts`, `board/boardTurn.ts` | |
| Goals | `docGoals.ts`, `goalLoop.ts`, `DocGoalChip.tsx` | `dashboard/doc_goal_loop.py` |
| Standing brief | | `doc_session_brief.py`, `context_assembly/agent_spec.py`, `dashboard/chat_turn/turn_admission.py` |
| Overlay | `docOverlay.ts` | artifact `doc-overlay-<id>` |
| Canvas access | `board/conductorGuard.ts`, `board/boardModel.ts` | |
| Pages opening pages | | `dashboard/doc_child.py`, `session_control_parts/session_create.py` |
| Mode and session view | `ConductorSession.tsx` | |

## Built and next

Built: doc and canvas pages in both modes; threads, goals, and the overlay for both;
the standing brief for docs; server-armed goals; pages opening pages with the depth cap;
the canvas guard on where additions land.

Next, as design:

- **The gateway applies a reply's file changes.** Writes are applied once per reply by
  the gateway, with the version check, so a conductor can change a page whose pane is
  closed. Today the open page applies them.
- **The brief moves into the canvas session's system prompt,** as it has for docs, so
  its turns also carry only what changed.
- **The mode is enforced on the server,** so a page cannot move between Plain and Crew
  after its first ask by any route.
- **The thread ledger and the filtered array** (Option B), starting with the long
  threads.
