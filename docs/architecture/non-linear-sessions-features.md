# Non-linear session types for orchestration

## Problem

Many people plan and track a larger effort as a document or a whiteboard: the plan
written out, the items side by side, how they relate drawn in. When that effort is run
by agents, there is no way to drive the session from that shape. The plan lives in
one place and the conversations that move it forward live in another, so keeping
them in step is the person's job.

## Overview

A Kiro Crew session is driven from a chat. You send a message, the agent answers, and
the conversation builds up in order. A session is a single thread, and what you see
of it is mostly the latest few messages. To give a view of the whole effort, we
optionally show a dashboard beside the chat. It is kept current from the session's
work ledger and crew log, and shows what is running, what is done and what needs you.

**Non-linear sessions** add two additional ways to drive a session. Each one is a
different surface on the session mechanism Kiro Crew already has, and each is still
one session underneath. You work on an object you can see all at once, and you can
hold several conversations in different places on it. Two surfaces ship:

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

- **The target is the surface itself.** "Help me write a doc", "help me lay this out on
  a canvas." The agent helps you as you create it, and the document or canvas is the
  work.
- **The target is somewhere else, and the surface directs it.** "Help me organize a
  plan of work and execute on it." The surface holds the plan, the items and how they
  relate, and the agent uses it to orchestrate work that lands elsewhere: branches,
  pull requests, tickets.

Both are ways of thinking about the surface, not settings. Every surface can be used
either way.

## What a surface is

A surface is a normal session with a **surface id**. When a session has one, the app
shows the surface instead of the chat.

The surface's content is one file: markdown for a doc, JSON for a canvas. Saves are
versioned, so a person's edit and the agent's edit never overwrite each other.

Everything else is an ordinary session: transcript, agent, memory, approvals, work
ledger and loops.

Every surface has the same two modes as a chat. They decide who does the work, not
where it lands:

- **Plain.** The agent is `kirocrew-worker`. It does the work itself, in its own
  session and worktree.
- **Ultra.** The agent is the conductor, `kirocrew-conductor`. It breaks the work up,
  starts workers, tracks them on its ledger and is woken when they report.

A new surface is Plain. The mode is chosen before the first ask and fixed after it.

## Features

That is the overview. The rest of this document goes feature by feature: what each
does, how the POC does it today, and the choices still open. The main ideas:

- A person talks to the session through the surface's [threads](#threads).
- The session answers through those same threads.
- The session also answers by changing the surface itself: adding text, shapes and
  sections.
- The session shows what is going on through an HTML
  [status overlay](#status-overlay) drawn over the surface.
- A person defines the work through the shape of the surface, and through
  [goals](#goals) set on its sections.
- A person decides which parts the session may change, through
  [surface access](#surface-access).
- A person or the agent can [open child surfaces](#surfaces-opening-surfaces), since
  each one is just another session.

## Threads

### The feature

You start a conversation anywhere on a surface: on a highlighted passage, at an empty
spot in a doc, or on a set of shapes on a canvas. Each one is a **thread**, anchored
where you started it. A doc shows a small pin at the anchor and opens the thread in a
side panel; a canvas pins a card where you pointed. You can have many open at once,
each about its own part of the effort, and the surface shows which have new replies.

### Why one session for the whole surface

Threads could each be their own session. They are not, because the unit is the
surface: one effort, owned by one session with one agent, ledger and set of workers.
That keeps the management of the work and agents apart from what is being talked
about. Threads are only where the person talks; the work belongs to the surface. The
options below change what that one agent is sent per turn, not that it owns the
surface.

### How the POC works today

The surface holds many threads, but the session underneath is one transcript. Two
things make that work: marking threads on the surface, and keeping them apart in the
transcript.

**On the surface.** The anchor is stored in the surface's file. A doc wraps the
highlight in hidden markers whose id is the thread id; a canvas lists each thread with
its shapes:

```
<!-- ask:a9ef6eq6 -->the highlighted text<!-- /ask:a9ef6eq6 -->
asks: [{ id: "a9ef6eq6", shapeIds: [...], at: {...} }]
```

The conversation never enters the file. What the agent changes on the surface does.

**In the transcript.** All threads share the one transcript, and the UI builds every
thread from it on each render. Messages say which thread they belong to with a prefix:

```
user       [Doc ask a9ef] what is this?
assistant  It is the pricing plan...
user       [Doc ask c42b] who owns this?
assistant  The billing worker...
nudge      [Goals] Checking the goal sections
assistant  [goal g1] Nothing new.
           [goal g2] Marked #26162 done.
```

- A person's message starts with `[Doc ask <id>]`, naming its thread.
- An answer to it has no prefix. It belongs to the thread of the message before it.
- A goal check answers several goals at once, so each part starts with `[goal <id>]`.

An answer can also carry fenced blocks that change the surface (`doc-insert`,
`canvas-add` and so on). The agent only adds, never rewrites.

**The system prompt.** The conventions are not repeated in each turn. They live in a
standing brief appended to the session's system prompt at start and after a
compaction: what each tag means, the reply blocks, that a turn without a document copy
means the last copy stands, where to read the file if the copy was lost. It has a Plain
and an Ultra variant. It names the surface only by id, because the system prompt must
stay identical across turns; the title and text arrive as events in the turns. Docs
have this today; canvases still carry their conventions in each turn.

### Options

The POC uses option 1. The question is where a thread's data lives and how the agent
says which thread it is acting on.

1. **Everything in the transcript, rebuilt on render.** Turns carry string tags, replies
   carry fenced blocks, and the UI parses the transcript to rebuild threads and apply
   writes.
2. **Threads are in transscript, tool calls that change a store, which updates the UI.** The transcript is still
   the one session, but the agent acts through tools instead of prefixed strings:
   `thread_reply(thread, text)`, `surface_insert(after, markdown)`,
   `canvas_add(section, shapes)`. Each call changes a store the gateway keeps (the
   threads, and the surface's file, with the version check), and the UI renders from
   that store instead of parsing the transcript.
3. **A thread ledger, with filtered threads sent to KAS.** The gateway stores messages
   by thread id and builds each turn's message array instead of continuing one
   transcript. It is still the surface's one session and agent; only what it is sent
   is filtered: the brief, the surface's current state, the goals' latest reports, worker
   reports, then only the messages of the thread being asked about.

In short: 1 is fast. 2 is likely more robust, but means storing state somewhere. With
3, threads become unaware of each other, which may be a danger.

## Goals

### The feature

A goal is a standing instruction on part of a surface: "keep these tickets' statuses
current", "watch this PR". You set it on a doc heading or a canvas section, and the
surface checks it on a schedule with no one asking, even while it is closed. Each goal
shows a pin on its section with its recent checks, and the results are written into
that section.

### How the POC works today

A doc keeps a goal as a hidden line under its heading, `<!-- goal <id>: text -->`,
covering the section down to the next heading of the same level. A canvas keeps it on
the section shape. A surface has one check rate for all its goals (5 to 60 minutes).

The gateway arms one ordinary auto-nudge loop on the surface's session from the saved
file, and keeps it in line on every save: arm, rewrite, revive or delete.

**The model does the checking.** The loop is only a timer. On each tick it sends the
model a prompt, and the model does the work: it reads the surface's file, looks at each
goal's section, uses its tools (checks a PR, reads a ticket, starts or steers a
worker), writes results into the section, and replies. Nothing is checked before the
prompt is sent. "Checking the goal sections" is just the label the transcript shows for
that prompt.

**Several goals are checked together.** A surface with three goal sections has one
loop, not three. Each tick sends one prompt, a `nudge` row, that covers all of them:

```
nudge      [Goals] Checking the goal sections
assistant  [goal g1]
           Marked #26162 done. #26182 still open, no PR yet.
           [goal g2]
           Nothing new.
           [goal g3]
           Started a worker on the billing page.
```

- **What the model is told.** The transcript shows only the banner. The model receives
  the full instruction, which does not list the goals. It gives the surface's file
  path and says how goals are written there (a `<!-- goal <id>: ... -->` line under a
  heading, or a `goal` on a canvas section). The model reads the file and finds every
  goal itself, so the instruction never goes stale as goals are added or removed.
- **What it does.** For each goal, it looks at that goal's section as it is now and
  does what the goal asks, writing only inside that section.
- **How it answers.** One reply, with one block per goal, each opened by a
  `[goal <id>]` line. A goal with nothing to report gets exactly `Nothing new.`
- **How the UI splits it.** The UI cuts the reply at the `[goal <id>]` lines and files
  each block in that goal's own thread, shown on its pin. Text outside any block goes
  to a shared goals thread.

### Options

The POC uses option 1. The question is how a goal's result becomes data the UI shows.

1. **Prefixed strings, parsed from the transcript.** The agent writes `[goal <id>]`
   blocks and `Nothing new.`; the UI parses them into per-goal threads and the check
   history on the pin.
   - Nothing new to store; consistent with threads option 1.
   - The history exists only as the UI's reading of the transcript, so anything else
     (a notification, another surface, a report) must parse it again.
2. **A tool call that updates a goal record.** The agent calls
   `goal_report(goal, status, summary)`, and the gateway stores each goal's latest state
   and history. The UI and any other reader read the record.
   - The result is structured: status, time, summary, linked work.
   - One record serves the pin, notifications and other surfaces alike.
   - Fits threads option 3, where the goals' latest reports go into every array.

## Status overlay

### The feature

The agent can draw status on top of a surface without changing its content: a running
spinner beside a task, a "needs you" badge on a section, a progress bar on a card, a
link to the worker doing the job. It turns a plan into a live map of the work, and it
stays out of the text, so the document reads clean and nothing the person wrote is
touched.

### How the POC works today

The overlay is one HTML artifact per surface (`doc-overlay-<id>`) that the agent owns
and replaces whenever the picture changes. Each top-level element is one item:

- `data-anchor` pins it to exact text in a doc, or to `shape:<id>` on a canvas;
- `data-place` puts it in the margin, over, at a corner, or on a side;
- `data-open-session` opens a session when it is clicked;
- ready-made classes carry state (`ov-running`, `ov-waiting`, `ov-done`, `ov-blocked`,
  `ov-failed`) and shape (`ov-card`, `ov-pill`, `ov-progress`).

The surface polls the artifact and draws it. Scripts, event handlers, forms and remote
resources are stripped, so the agent can lay out an item freely but cannot run code on
the surface.

### Options

The POC uses option 1.

1. **One HTML artifact, anchored by attributes.** Free layout for the agent and no new
   storage, but the agent rewrites the whole overlay to change one item, a stale
   anchor silently drops its item, and the state is only legible as HTML.
2. **Structured status items through a tool.** The agent calls
   `status_set(anchor, state, label, session?)` and the gateway stores the items; the
   surface renders them with fixed components.
   - One item changes at a time, and an item can be validated against the surface when
     it is set.
   - The same records feed notifications, the dashboard beside a chat, and a parent
     surface showing its children.
   - Less freedom of layout than HTML.
3. **Both.** Structured items for state, with an optional HTML body for the rare item
   that needs custom layout.

## Surface access

### The feature

A person decides which parts of a surface the agent may change. A whole surface can be
closed to the agent by default and opened section by section, or open with some
sections protected. The agent can still read everything and talk about it; it just
cannot change what is protected. This lets a person keep their own writing, a signed-off
plan or a shared section safe while the agent works on the rest.

### How the POC works today

Each container carries an optional setting, `allow` or `deny`, and the surface carries
a default. The nearest container with a setting decides, walking out from the
innermost, then the surface's own default. On a canvas the containers are sections;
on a doc they are headings, with the setting kept as a hidden line under the heading
like a goal.

The agent is told what it may change in every turn, but telling is not the
enforcement. Every write is filtered before it lands: whatever touches a protected
container is dropped and named to the person. An addition is judged twice, once for
where it was asked to go and again for where it actually landed, because content can
be asked for in one place and placed in another.

In the POC the canvas has this, and the filter runs in the browser when the reply is
applied. With writes as tool calls (threads option 2), the same check runs in the
gateway at the call, which also covers a closed surface and fails back to the agent
instead of dropping silently.

## Surfaces opening surfaces

### The feature

In Ultra Mode a conductor can open another surface for a piece of work big enough to
need its own: a canvas plan opens a doc for one section, with its own threads, goals
and agent. The new surface is linked from the one that opened it and reports back
when its work moves.

### How the POC works today

The conductor calls `session_create` with the kind and the new surface's starting
content. The new session is bound to a fresh surface id, its file is written at once,
and it runs Plain unless an agent is named. A goal in the content arms its loop
immediately. The parent briefs it, records it on its ledger and links it from its own
surface; the child reports back with `work_report`. Only a surface's conductor may do
this, and a chain stops two levels below the surface a person made, counted through
each session's recorded creator.
