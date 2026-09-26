# Session Control — driving another session

One chat session can open, fork, seed, watch, stop and close another one, and take
another one under itself in the sidebar. The tools come from the
`kirocrew-dashboard` MCP server, so an agent that does not mount that server
never has them — exactly like any other MCP server. This page is the reference
for all 18 of its tools, written for the agent that is about to use them.

The server is defined in `src/kiro_crew/mcp_dashboard.py`. Two halves:

- **Session control** — `session_create`, `session_fork`, `session_send`,
  `session_read_message`, `session_stop`, `session_close`, `session_adopt`,
  `session_release`. These reach another session.
- **Sidebar shape** — `chat_folder_tree`, `chat_folder_create`,
  `chat_folder_move`, `chat_folder_move_session`, `chat_folder_file_self`,
  `chat_folder_steering_set`,
  `chat_tag_list`, `chat_tag_create`, `chat_tag_update`, `chat_tag_assign`.
  These organize what the person sees in the sidebar.

Everything a created session does is visible: it appears in the user's sidebar
like any other tab, they can read it, take it over, and close it. This is how
you stand up a workstream alongside your own, not a way to hide work.

## The one mistake to avoid

`session_create` takes `agent`, and **omitting it inherits the CALLER'S OWN
agent** — not a global default. The schema says so in as many words:

> Omitting it inherits the CALLER'S OWN agent, not a global default:
> `create_session` falls back to the calling slot's agent so the child stays in
> this workspace's memory boundary. A conductor that omits it therefore gets a
> second conductor, which has no `fs_write` and cannot do the work. Name the
> agent the child needs explicitly — `kirocrew-worker` for a leaf work item.

So a conductor that calls `session_create(title="fix the build")` gets another
conductor: a session that can dispatch but cannot edit a file. Always pass
`agent` explicitly.

## Session tools

### `session_create`

| Argument | Required | Meaning |
|---|---|---|
| `title` | no | Short sidebar name. Say what the session is FOR |
| `agent` | no (but always pass it) | Agent to bind the session to |
| `folder` | no | Sidebar folder id or `/`-separated path to file it into, atomically with creation. Missing path segments are created (`mkdir -p`) |

No argument is formally required. The new session **starts empty** — nothing runs
in it until the person types, or until you send it a message. The reply carries
the new session's key:

```
🆕 Opened `chat-7` (fix the build) filed in `Build work`. It is empty and
waiting in the user's sidebar; watch it with session_read_message.
```

Pass that key as `target` to every other session tool.

`session_create` always starts empty. When the new session should already know
what you know — you are splitting a long investigation into several sessions —
use `session_fork` instead.

### `session_fork`

| Argument | Required | Meaning |
|---|---|---|
| `source` | no | The session to copy from: a session key, slot key, or its exact unique title. Omit to fork **your own** session |
| `title` | no | Short sidebar name. Omit to keep the fork's own `Fork of <source title>` |
| `folder` | no | Sidebar folder id or `/`-separated path, created if missing (`mkdir -p`), as for `session_create`. Omit to leave the child in the source's folder |
| `at_message_index` | no | Fork point: the position of the LAST message to carry, counting the source's user and assistant messages from 0. Omit to carry the whole transcript |

The same thing as the dashboard's Fork button, for an agent. The new session
**carries a copy of the source's transcript** up to and including the fork
point, so it starts with the context already built instead of empty. It starts
**idle**: the copied messages are history, and nothing runs until you
`session_send` into it or the person types. It appears in the sidebar for the
person to read, take over and close, like any other session.

```
🔀 Forked `chat-3` into `chat-9` (cluster 3: duid) carrying 41 message(s) filed
in `Gamma failures`. It is idle and waiting in the user's sidebar; seed it with
session_send and watch it with session_read_message.
```

Two things it deliberately does not take:

- **No `agent`, `model` or `mode` override.** The child inherits the source's
  agent, model, memory store and mode, project and folder — exactly what a human
  fork inherits, and for the same reason: a transcript belongs to the memory
  boundary it was written in, and a fork must stay inside it. A session bound to
  a different agent is `session_create`'s job.
- **No tail fork.** Only the head (everything up to the fork point) is offered.

Forking another session is a **read** of its transcript, so naming a `source`
that is not your own needs exactly what `session_read_message` needs on that
session — same fence, same refusal codes. Either way you must be an eligible
creator: every caller class `session_create` refuses, `session_fork` refuses
too, because a fork manufactures a session you then own. Naming your own key as
`source` is the default case spelled out, not a `self_target` refusal.

Refusals minted by the fork itself come back under the fork's own codes, the
same ones the dashboard's Fork button reports: `value_out_of_range` for a fork
point past the source's last visible message, `no_messages_to_fork` for a
source with no user or assistant messages yet, and `fork_snapshot_unstable`
(retry) for a source that is being written to.

The child is attributed to you (`created_by`), so `session_send`,
`session_read_message` and `session_close` reach it afterwards, and it counts
against your creation budget and per-creator ceiling exactly as a created
session does.

### `session_send`

| Argument | Required | Meaning |
|---|---|---|
| `target` | yes | Session key, or its exact title |
| `message` | yes | Becomes the target's next user-role turn |
| `steer` | no (default `false`) | Cut into the turn already running |

Three outcomes, and the reply tells you which one happened:

| Target state | `steer` | What happens |
|---|---|---|
| idle | either | It starts a turn on your message straight away |
| busy | `false` | Queued; it runs when the current turn ends |
| busy | `true` | Injected into the running turn, so the target reads it mid-work |

A steer that cannot be injected **falls back to the queue** rather than being
dropped, and the reply says that explicitly. `steer` is ignored on an idle
target, because the message starts a turn either way.

Use `steer=true` only when waiting would waste the work in flight — the target
is heading the wrong way, or the thing it is grinding on is already done.

The message lands in the target's transcript tagged as sent by your session, so
the person reading it can tell it from their own typing.

### `session_read_message`

| Argument | Required | Meaning |
|---|---|---|
| `target` | yes | Session key, or its exact title |
| `limit` | no (default 20, max 100) | Max messages to return |
| `since` | no | Return messages from this index onward |

Read-only: it sends nothing and changes nothing about the target. This is how you
read a transcript **without joining it** — no message of yours appears in the
target, and the person's tab is untouched.

The reply opens with a state line and closes with a cursor:

```
📖 `chat-7` — fix the build (still working, 2 message(s) queued; total=31)
[29] assistant: ran the build, 3 failures left
[30] user: keep going
Pass since=31 on your next read to see only what is new.
```

Poll by passing the previous read's `next_since` back as `since`, so a loop does
not re-read what it already saw. Two readings matter:

- `running: false` with nothing new means the target finished and is idle. That
  is the difference between "not done yet" and "done".
- `total` is the backlog depth. When it exceeds `next_since` there are older
  rows this window did not reach — read again immediately instead of waiting.

`wait`, then read. See [Monitor loops](monitor-loops.md) for the loop shape.

### `session_adopt` and `session_release`

Both take only `target`. They change where a session sits in the sidebar and
nothing else: the target keeps its conversation, its turns, its tools and its
agent.

`session_adopt` puts the target under YOU. The adopter is the calling session,
resolved from the connection — there is no argument for it, so no session can
rearrange a part of the tree it is not in.

The case it exists for is a takeover. Open one session, adopt each of the
sessions you are now running, and the workers THEY opened come along with them:
a session hangs under another session, not under a path, so one call moves a
whole branch. A target that already has a parent can be adopted, and the parent
it had is kept in the record.

| Refusal | Why |
|---|---|
| `would_cycle` | The target is already above you, so the tree would hold a loop. A loop is a shape the tree cannot show — it marks every session on it and nests none of them — so this would flatten a branch rather than move it. |
| `already_root` | On release only: the target has no parent, so there is nothing to let go. |
| `not_parent` | On release only: the target hangs under a third session. Only that session, or the target itself, can release it. |
| `tree_unavailable` | The session tree is not being recorded on this gateway, so there is nowhere to write the edge. Nothing moved, and the tool says so rather than reporting a success the sidebar will not show. |
| `tree_not_ready` | The tree cannot be read whole right now — the gateway has not seeded it yet, or a unit's log could not be read. Retryable: a decision taken on a partial tree could admit the loop `would_cycle` exists to refuse, so it is refused instead of guessed. |
| `tree_write_pending` | An earlier move of this same session is still being written. Retryable: read the tree first, because the earlier write may have landed. |

`session_release` is the only way to undo an adoption. You may release a session
you hold, and you may release YOURSELF — pass your own key — so a session whose
holder has stopped running is not stuck under it. Sessions the released one
holds stay with it: only its own edge upward goes.

Both are recorded in the target's own crew log, on the side the creating edge is
already written on, which is why the whole subtree follows with no entry of its
own.

### `session_stop` vs `session_close`

Both take only `target`. They are not interchangeable.

| | `session_stop` | `session_close` |
|---|---|---|
| What it does | Cancels the in-flight turn, like pressing Stop in that tab | Dismisses the tab, like pressing ✕ |
| The tab afterwards | Still open, idle | Archived to history, reopenable |
| A running turn | Cancelled, its work discarded | Cancelled first, then archived |
| Queued messages | Kept by the first stop; an escalated stop clears them | Saved with the archive and handed back when the conversation is reopened |
| Use it when | A peer is working on something wrong or already done | You are finished with a peer session you created |

`session_stop` is **cooperative and safe to re-send**. A repeat within
`stop_retry.WINDOW_SECS` (120 seconds) of your own first stop of that target
returns the existing "stop already in progress" no-op instead of escalating to a
hard kill — because a hard kill discards the target's queue and pending steers,
and a repeat cannot be told apart from a retry of a request that timed out. A
stop arriving after that window still escalates, so a genuine second decision
keeps the capability.

The reply distinguishes the two facts a stop can report: `nothing to stop` for a
target that was never running, and `the earlier stop still stands` for one whose
cancel is still in flight.

`session_close` is not a permanent delete — the conversation is archived and can
be reopened — but it does discard a running turn's work. Read the session first
when you are not sure what it is doing.

## Folders

The sidebar tree the person organizes their sessions in.

| Tool | Arguments | What it does |
|---|---|---|
| `chat_folder_tree` | none | Every folder (id, human path, project dir, default agent, declared steering directories) with the live sessions nested under it, plus an `(unfiled)` group. Listed in **sidebar order**, not alphabetically |
| `chat_folder_create` | `name` (required), `parent` | Create a folder. `parent` is an id or a `/`-separated path; missing segments are created (`mkdir -p`). Omit or pass `root` for top level. Creating never moves anything |
| `chat_folder_move` | `folder` (required), `new_parent`, `before`, `after` | Reparent a folder and/or set its position among siblings. Moves everything inside it; cycle-guarded |
| `chat_folder_move_session` | `session` (required), `folder` | File another live session into a folder, or omit `folder` to unfile it to the top level |
| `chat_folder_file_self` | `folder` | File **this** session — the caller — into a folder. Writes only its own placement |
| `chat_folder_steering_set` | `folder` (required), `steering_dirs` (required) | Replace a folder's **additional steering directories** — the same setting as Folder settings → Additional steering, through the same endpoint. `[]` clears |

Read `chat_folder_tree` before you move anything: it renders folders in the order
the person actually sees, which is what makes a `before` / `after` anchor safe to
pick. `before` and `after` are mutually exclusive — one anchor names one
position — and the anchor must be a sibling. With an anchor and no `new_parent`,
the anchor chooses the parent, which is how you reorder a folder without moving
it.

A folder `name` cannot contain `/`: a folder named `A/B` renders identically to
`B` inside `A` and becomes unaddressable by path. Names are capped at 100
characters, checked after credential redaction.

`chat_folder_file_self` is the verb a conductor wants: file yourself in the
goal's folder first, then create each worker with
`folder="<goal>/<worker agent>"`, and the person finds the conductor and every
worker under one heading. It can write no placement but its own, which is why it
is safe to grant where `chat_folder_move_session` is withheld.

`chat_folder_steering_set` is how an agent operates the folder's steering the way
the person does from Folder settings. Every always-inclusion `*.md` under each
directory is delivered to every session filed in the folder or its subfolders,
after whatever the ancestor folders declare (steering accumulates root-first;
`chat_folder_tree` shows each folder's own `steering=[…]`, so the ancestors'
lines above are the inherited set). The list you pass REPLACES the stored one.
Each entry must be an absolute path to an existing directory; a sensitive path,
a duplicate, or more than 16 entries is refused by the endpoint
(`steering_dirs_invalid`), and native Windows refuses any non-empty list. Only
the person may declare steering: a steering directory is a host-file read the
gateway performs on the folder's behalf, and folder permission is not host-file
permission — so an app or crew-member session is refused at the tool for setting
and clearing alike (the endpoint's `steering_dirs_forbidden` covers only a non-empty
list, and would let it clear a folder it owns), a channel- or schedule-bound session is refused at
the tool before any read, and the verb is accepted only from the person's own
dashboard tab — for setting and clearing alike, and only while that tab has no
channel link and only in a conversation no channel message has entered. A
`dashboard:` key names the slot a turn runs in, not where its words came from: a
tab linked to a Slack thread (`/kirocrew link-to-dashboard`) keeps its key and
runs the thread's replies as turns, and a channel-born session is brought back
by a human reply the same way. Once such a reply is in the conversation, any
later turn can act on its text — a follow-up turn, a subagent's result, a turn
you type after unlinking — so the conversation is what gets marked, not the
turn. The slot records `channel_turn_seen` the moment a channel message (or a
scheduled job, app or crew-peer turn) is queued or run, and never clears it. It
is not saved to disk: after a gateway restart, a History resume or an import,
every restored conversation reads as marked, because the saved transcript can be
edited and cannot prove a conversation clean. So a tab that worked before a
restart is refused after it; open a fresh tab for the change. The tool
refuses on it. A second, independent refusal is the slot's shape: any channel
link on the caller's own slot refuses the verb even before a reply has
arrived. Either way the fix is a fresh dashboard tab, or Folder settings →
Additional steering.

Even from a clean tab, every change the tool asks for waits for the person: an
approval card appears on the dashboard naming the folder and the directories,
and nothing is stored until the person clicks Approve. No trust or yolo mode
answers it, and the agent cannot answer its own card. A declined card, or one
left unanswered for about five minutes, returns `steering_approval_declined`
and stores nothing. The gates above cannot see a web page or a file the agent
read into the conversation, so the person's click is what authorizes the
change. Clearing is not exempted
because the stored list keeps no prior value: an erased list is config the person
must retype, and an empty list is an ordinary input from a channel session, not
a rare one.

Folder moves are metadata only: the session keeps its transcript, its model, and
any running turn. Archived (history) sessions cannot be moved — revive one into
the sidebar first. There is no delete verb here.

## Tags

One shared vocabulary of labels. A **status tag** is what a Trello-style column
filters on, so a session normally carries one at a time.

| Tool | Arguments | What it does |
|---|---|---|
| `chat_tag_list` | none | Every tag's id, name, color, and whether it is a status tag. Read-only |
| `chat_tag_create` | `name` (required), `color`, `status` | Create a tag. `name` matches case-insensitively against existing tags, so calling it for one that exists is a safe no-op |
| `chat_tag_update` | `tag` (required), plus at least one of `name`, `color`, `status` | Rename, recolor, or toggle the status flag |
| `chat_tag_assign` | `session` (required), `add`, `remove` | Add and/or remove tags on a live session |

`chat_tag_assign` is a **delta**: tags you do not name are kept. It is applied
compare-and-set against the tag list the call read, so if the person toggles a
tag at the same moment, the call fails with the current list instead of
overwriting their click — re-read and retry. At least one of `add` / `remove`
must be non-empty, and a tag must already exist.

Tag names are capped at 60 characters, checked after redaction. **This server can
never delete a tag**, so nothing here can lose a label the person put on a
session. `chat_tag_update` is metadata only: every session carrying the tag keeps
it, and a column filtering on it keeps filtering.

## What you cannot reach

Session control authorizes on the **calling session's identity**, and only a
gateway-issued key counts. Refusals you should expect, by code:

| Code | Meaning |
|---|---|
| `target_not_found` | No open session matches that key or title. A closed tab is out of scope |
| `ambiguous_target` | The string matches more than one session across the three forms below. Address it by its session key |
| `self_target` | A session cannot control itself |
| `not_creator` | The caller is fenced to sessions it created itself (a crew member's DM slot, a scheduled run, and anything either of them created) |
| `workspace_mismatch` | Peers must be in the same workspace — that is the memory boundary |
| `ephemeral_target` | Incognito and temporary sessions are not addressable |
| `app_scoped_target` | App-scoped sessions are not addressable, in either direction |
| `unattended_caller` / `unattended_target` | Scheduled runs cannot be controlled; a cron caller reaches only what it created |
| `linked_session_target` / `mirrored_target` | A channel-linked or channel-mirrored session is out of scope — reaching it would cross into a thread other people read |
| `session_control_disabled` | `agent.session_control` is off in config |
| `create_rate_limited` | Per-caller creation budget spent — a fork spends the same budget |

`target` resolves three ways, all of them checked before any answer: the slot
key (`chat-7`), the transcript name `list_sessions` prints
(`dashboard_chat-7`), and the session's exact title, matched
case-insensitively. A string that matches two DIFFERENT sessions across those
forms is refused rather than guessed at — `session_stop` discards a live turn's
work, so picking the wrong conversation is the one outcome resolution must never
produce. Pass the key when a title might collide.

A **spawned subagent has no gateway-issued key of its own**, so every
session-control tool refuses it: its identity would resolve to its parent slot,
handing it the parent's authority. Drive sessions from a real session, not from
inside a subagent.

A **channel agent** (Slack, Telegram, and the rest) is blocked from all eight
session tools by `CHANNEL_AGENT_BLOCKED_TOOLS` in `src/kiro_crew/channel.py`.
Reading a dashboard transcript would pull a private conversation into a channel
other humans can see, and sending would run channel text as a turn inside it.
`chat_folder_steering_set` is on the same list: it names host directories the
gateway reads into every later chat in the folder, on words from a thread other
people are in.

### Switches and ceilings

| Knob | Default | Effect |
|---|---|---|
| `agent.session_control` | `true` | The whole surface. Turn it off to withdraw the capability from every agent at once without editing a spec |
| `agent.member_dispatch` | `true` | A crew member's DM session drives workers it created even when session control is off. Turn it off to put member callers back under the switch |
| `agent.crew_panel` | `true` | A crew member publishes its own webview, shown in that member's drawer on the Crew page. Its own mount and its own switch, so withdrawing session control leaves the drawer alone and withdrawing the drawer leaves session control alone |

Rate limits are per caller, per verb, over a 300-second window: **20** session
creates (forks included), **10** folder creates, **10** tag creates. Capacity ceilings sit behind
them: 500 live sessions, 50 per creator, 500 folders.

## This or `spawn_run`?

Both run work in parallel. They differ in who owns the result and whether the
person can see it happening.

| | Session control | `spawn_run` |
|---|---|---|
| Where the work lives | A real session in the user's sidebar | A background subagent process |
| Visibility | The person reads, steers and takes over the tab | A completion event injected into your turn |
| Lifetime | Until closed; survives your session ending | Bounded by the spawn timeout |
| Two-way traffic | `session_send` in, `session_read_message` out, any number of rounds | Task in, result out; `spawn_steer` for one injection |
| Nesting | A created session can create its own | Subagents cannot spawn subagents |
| Cost of a mistake | A tab the person can inspect and stop | A run you re-do |
| Reach for it when | A workstream needs its own identity, a long life, or the person's oversight | You need fan-out, distillation of a large input, or a blind second opinion |

Rule of thumb: **`spawn_run` for answers, session control for workstreams.** A
parallel code review is a spawn. A worker that will grind for an hour, report,
get corrected, and report again is a session.

## Related

- [Subagents & parallel work](subagents.md) — `spawn_run` and the solo gate
- [Dashboard](dashboard.md) — the sidebar, tabs and queued messages these tools write to
- [Monitor loops](monitor-loops.md) — the `wait`-then-read shape a watcher uses
- [Configuration](configuration.md) — where `agent.session_control` lives
