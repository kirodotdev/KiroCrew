# Session Control — driving another session

One chat session can open, fork, seed, watch, stop, close and revive another one,
change its model, reload its agent process, and take another one under itself in
the sidebar. The tools come from the
`kirocrew-dashboard` MCP server, so an agent that does not mount that server
never has them — exactly like any other MCP server. This page is the reference
for all 29 of its tools, written for the agent that is about to use them.

The server is defined in `src/kiro_crew/mcp_dashboard.py`. Two halves:

- **Session control** — `session_create`, `session_fork`, `session_send`,
  `session_read_message`, `session_summary`, `session_stop`, `session_end_wait`,
  `session_set_model`, `session_reload`, `session_close`, `session_revive`, `session_broadcast`,
  `session_status`, `session_adopt`, `session_release`. These reach another session.
- **Sidebar shape** — `chat_folder_tree`, `chat_folder_create`,
  `chat_folder_move`, `chat_folder_move_session`, `chat_folder_file_self`,
  `chat_folder_steering_set`,
  `chat_tag_list`, `chat_tag_create`, `chat_tag_update`, `chat_tag_assign`,
  `chat_tag_column_list`, `chat_tag_column_create`, `chat_tag_column_move`,
  `chat_session_pin`. These organize what the person sees in the sidebar.

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
| `model` | no | Model the session starts on, pinned as if the person picked it in the model dropdown (same guard; refused with `model_rejected` when the picker would refuse it). Omit for the agent's or global default |

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

### `session_broadcast`

One message, several sessions. Use it when the thing you have to say is true of
every worker rather than of one: the base moved, the approach changed, stop.

| Argument | Required | Meaning |
|---|---|---|
| `message` | yes | Becomes each target's next user-role turn |
| `mode` | yes | `queue` or `steer`. No default |
| `targets` | no | Session keys or titles. Omit to reach every session you created |

`mode` is required because the two are different instructions, not two settings
of one. `queue` lets every target finish what it is doing; `steer` cuts into
every running turn. "The base moved, rebase before you push" is the first. "Stop,
that issue is already fixed" is the second, and it is worth interrupting eight
turns for. A default either way would silently pick one of those for a caller who
meant the other.

Each delivery goes through the same checks a single `session_send` does, so a
broadcast can reach nothing a `session_send` could not — including the ownership
fence, which is why the default audience (what you created) is always within
reach.

**Partial delivery is normal.** Deliveries happen one at a time, and a target
that was closed, went incognito, or belongs to an app is one refused row while
the rest still get the message. A target that simply never answers is one row
too: each delivery has its own few-second bound, and on expiry the broadcast
stops waiting for that session and moves to the next. The reply is per target:

```
📣 Queued for 2/6 session(s):
  ✅ `chat-7` — queued until its turn ends
  ✅ `chat-8` — started a turn on it
  ❌ `chat-9` — no open session matches 'chat-9' (target_not_found)
  ❌ `chat-10` — timed out; pending or queued, do not re-send (delivery_timeout)
  ❌ `chat-11` — timed out before hand-over, safe to re-send (delivery_timeout)
  ❌ `chat-12` — timed out and unavailable to inspect, outcome unknown (delivery_timeout)
Some targets were not reached — the rows above say which and why. Nothing
retries them for you.
```

Read the rows. Treating the call as all-or-nothing is how a worker silently
misses an instruction its siblings acted on. Nothing retries a refused row, and
at most **50** sessions are reachable in one call — an audience over that is
refused rather than truncated, because a silently-cut broadcast is one you
believe reached everyone.

`delivery_timeout` means only that this side stopped waiting. The gateway
records the delivery's own progress instead of reconstructing it from target state:

- **Before authorization:** the steer await was never entered, so the delivery did
  not reach hand-over. Re-sending the same text is safe.
- **Inside or after the steer await:** the outcome is unknown. Do not re-send; the
  instruction may already have executed.
- **Pending or queued:** retained text is additional evidence that it may still
  run. Do not re-send it; a duplicate could run too. Empty containers do not prove
  the opposite because successful delivery clears them as it progresses.
- **Target unavailable:** the gateway cannot inspect the original slot. The
  outcome is unknown, and the row advises neither action.

Every timeout row avoids claiming delivery or certain execution. Cancellation at
the SEL and configuration prewarms remains distinguishable from cancellation in
or after the steer await.

The target's transcript tags the message as a broadcast rather than a plain send,
so a worker can tell an instruction its siblings also received from one aimed at
it alone. That difference changes what the worker should do: the same sentence is
a fact about its own branch in one case and a fact about the base in the other.

### `session_status`

Takes no arguments. The roster of sessions you stood up, and what each is doing.

| `status` | Meaning | What to do |
|---|---|---|
| `working` | A turn is in flight | Wait |
| `queued` | Idle, messages waiting to run | Wait; a steer would land on nothing |
| `idle` | Open and doing nothing | This is the one that needs a decision |
| `gone` | The crew log has it, the dashboard does not | Re-dispatch or drop it |
| `unknown` | History has its birth metadata, but no live slot or crew-log edge exists | Inspect or re-dispatch without assuming it finished or was lost |

`gone` and `unknown` are why this tool exists rather than reading sessions one at
a time. Live slots forget a session the moment it is closed or lost with the
process that ran it. The crew log preserves a gateway-attested edge after a
session starts its first turn. History metadata fills the earlier window because
`session_create` persists `created_by` at birth. A row's `source` names exactly
which evidence placed it: `crew_log`, `history`, `live`, or a `+`-joined
combination. A history-only row stays `unknown` because editable transcript
metadata proves the creator but cannot prove why the live slot is absent.

The ownership fence applies to all three sources. A history row is shown only
when its persisted `created_by` exactly names you and its workspace matches
yours, so another caller's archived session and title do not appear.

A LIVE row passes the same target containment `session_broadcast` resolves names
against — one shared predicate, not a second copy of its clauses. So a session you
created contributes no title and no live status once it becomes something you
could not message anyway: bound to a channel thread, mirrored into one,
app-scoped, ephemeral, or moved to another workspace. Rows carry titles, and a
linked session's title is derived from a conversation other people are in.

The containment covers LIVE rows. A session the attested tree recorded still
leaves its titleless `gone` row once its slot is gone, which carries no title and
no live state — it says only that a session on your own tree is not live. That
tree follows the CURRENT parent edge, so it covers a session you adopted as well
as one you created.

The two quality fields describe the durable sources independently:

| Field | Value | Meaning |
|---|---|---|
| `tree` | `readable` | The crew-log fold was read completely |
| `tree` | `incomplete` | Crew-log rows may be missing; its contribution is a floor |
| `tree` | `unreadable` | No crew-log roster was available |
| `history` | `readable` | The transcript metadata catalog was read completely |
| `history` | `incomplete` | History rows may be missing; its contribution is a floor |
| `history` | `unreadable` | No transcript metadata roster was available |

Neither field alone says the combined count is exact. A short list under an
`incomplete` or `unreadable` value is not evidence that you created nothing.
Read-only: it sends nothing and changes nothing.

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

### `session_summary`

| Argument | Required | Meaning |
|---|---|---|
| `target` | yes | Session key, or its exact title |

Returns the target's intent summary, the digest the dashboard's summary panel
shows: each goal with its status, recent progress and next steps, then the
session's recurring project notes. The reply is bounded: the ten most recently
touched goals, the last five progress items and first five next steps of each,
ten notes, and 500 characters per line, with a count of anything it left out. Use it when a patrol cycle needs to know what
a peer is doing and a transcript tail would cost 20 rows to find out.

```
🧭 `chat-7` — fix the build (idle)
Summary written 2026-09-29T21:40:12Z.
- [in-progress] Get the build green
    progress: 3 of 5 failing tests fixed
    next: fix the two remaining snapshot tests
```

It is a cached read and never spends a model call. The dashboard writes the
summary at turn end, and only when the operator has turned on
`session_summary.enabled`, so three answers are normal:

- Summaries are off: the reply says so. Use `session_read_message`.
- No summary yet: a session with too few turns has none.
- `STALE`: turns have landed since the summary was written. The digest still
  describes the session, but anything newer is only in the transcript.

It is authorized exactly as `session_read_message` is, because the summary is
derived from the same transcript: a session you cannot read, you cannot
summarize. The check runs again after the summary is read, so a session that
gains a channel link or is replaced in between is refused (`target_replaced`)
rather than answered. An incognito session never has one. Read-only.

Each goal carries the panel's state word (`in-progress`, `needs-you`, `done`,
`dropped`), so work that finished without being verified reads `needs-you`
rather than done.

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

`session_close` is not a permanent delete — the conversation is archived and
`session_revive` brings it back — but it does discard a running turn's work. Read the session first
when you are not sure what it is doing.

### `session_end_wait`

| Argument | Required | Meaning |
|---|---|---|
| `target` | yes | Session key or exact title |

Wakes a session that is sleeping in the `wait` tool, the same way the End wait
button on its countdown does. The turn is not cancelled: the target's `wait`
returns a normal result, and the turn carries on from there. Use it when a
worker is waiting for something that has already happened, such as a build you
watched finish. It reaches only sessions you created; any other target is
refused with `not_creator`, even for a caller the other verbs do not fence.

The target's `wait` result names your session (`Wait ended early by session
<key> (session_end_wait) ...`), so the woken agent can tell that a peer ended its
sleep rather than the person or a steer. The wake lands on the target's next
keepalive ping, within about five seconds.

A target that is not sleeping in `wait` gets an informational reply ending
`Nothing to end.`, not an error, so there is nothing to retry. The same reply
comes back when two sleeps share the target's session key: the dashboard cannot
tell which one to wake, and hides the button for the same reason.

Compared with the verbs next to it: `session_stop` discards the turn's work,
and `session_send` to a sleeping target also ends its wait (as a steer) but
delivers text the target then acts on. `session_end_wait` does neither.

### `session_set_model`

| Argument | Required | Meaning |
|---|---|---|
| `target` | yes | Session key or exact title |
| `model` | yes | Model to switch to: a canonical key or provider id, such as `sonnet` or `opus` |

The model is recorded as a pending pick and applied when the target's next turn
starts. At that point the same permission check runs again, in the same step
that sets the model. If the target has become channel-linked, mirrored or
otherwise out of reach, the pick is dropped and the turn runs on its old model.
A later call replaces a pick that has not been applied yet, and a model the
user picks in the meantime wins over it. The conversation is kept.
`session_read_message` shows the target's current model and any pick still
pending, so you can tell whether yours took. A pending pick does not survive a
gateway restart.

Only an idle session takes a pick. If the target has a turn or sub-agents in
flight, the call is refused with `target_busy` ("session busy, model not
changed") and the target keeps its model. The tool never stops anything itself.
To force the change, call `session_stop`, then retry once the target is idle.

Also refused: `auto` and `Auto (Jev)` (`model_owner_only`), because with the Jev
preview on a slot on `auto` hands each turn's model to Jev routing, which only the
owner may arm; and a target bound to a remote crew (`remote_target_unsupported`).

### `session_reload`

| Argument | Required | Meaning |
|---|---|---|
| `target` | yes | Session key or exact title |

Relaunches the target's agent process, the same as **Reload session** in that
tab's menu. A running session builds its tool table once, when its process
starts, so a newly granted or enabled MCP server, an MCP config edit or an
agent-spec fix only reaches it after a reload. The new process re-reads its
agent spec, environment and MCP servers and resumes the same conversation. The
transcript is not rewritten; one reload notice is appended, and it names the
calling session's key so the person reading that tab can see who reloaded it.
The agent, model and workspace do not change.

Only sessions you created can be reloaded, whatever kind of session you are.
Only an idle one: a turn running or starting, queued messages, or attached
sub-agents refuse the call with `target_busy` ("session busy, not reloaded")
and nothing is torn down. The tool never stops anything itself. You cannot
reload yourself (`self_target`): the calling session is mid-turn, and a reload
under a running turn would orphan it. A target bound to a remote crew is refused
with `remote_target_unsupported`.

If the target gains a channel link or otherwise leaves your reach while its
process is being reset, the call answers `target_changed_during_reload`: the
old process is already gone and will start again on the target's next message,
but no reload notice was written.

If the old process fails while shutting down after it was removed, the reload
still counts as done: the notice is written, the process starts again, and the
reply carries a `warning`. A failure before anything was removed answers
`reload_failed`, and nothing was torn down.

### `session_revive`

| Argument | Required | Meaning |
|---|---|---|
| `target` | yes | The archived session: its slot key (`chat-7-...`), its `dashboard:<slot>` session key, the transcript name `list_sessions` reports, or its exact title when it is unique among archived sessions |
| `folder` | no | Sidebar folder id or `/`-separated path to file it into once it is live (best-effort; the result's `filed` says whether it happened). Missing path segments are created (`mkdir -p`), like `session_create`'s `folder` |

The mirror of `session_close`: it pulls a history session back up into a live
tab with its full transcript — the same thing as clicking it in the History tab.
Nothing runs until someone sends it a message, so the revived session is idle
and addressable by the key the reply returns: `session_send`,
`session_read_message`, `chat_folder_move_session` and `chat_tag_assign` all
work on it afterwards. Reviving does not transfer ownership — the session keeps
the creator it had — and a session that is already open is refused with its
live key (`target_already_live`) rather than opened twice.

The same containment as the other verbs applies, read from the archived
session's own metadata since there is no live slot yet: same workspace only, no
app-scoped or channel-linked sessions, and an ownership-fenced caller (a crew
member, a scheduled run, an agent-created session) may revive only a session it
created itself. Because the archived metadata line is a file an agent's tools can
edit, that ownership is corroborated against the crew log's session-tree lineage,
which only the gateway writes: a fenced caller's revive is refused
`ownership_unverified` when the lineage is off or names a different parent. The
crew log is on by default (`KIROCREW_CREW_LOG` unset, empty or truthy); a fenced
revive is refused when the log is off, its projection is unseeded, or the session
predates it, since none of those leaves a lineage to check. The person's own sessions are not ownership-gated and are
unaffected. A revive also spends the caller's create budget and per-caller slot
cap (the revived slot is charged to the reviver for the cap while keeping its own
creator) and the global slot cap.

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
answers it, and only the owner's own dashboard session can answer the card, so
the agent cannot answer its own. A declined card, or one
left unanswered for 90 seconds, returns `steering_approval_declined`
and stores nothing. The gates above cannot see a web page or a file the agent
read into the conversation, so the person's click is what authorizes the
change. Clearing is not exempted
because the stored list keeps no prior value: an erased list is config the person
must retype, and an empty list is an ordinary input from a channel session, not
a rare one.

Folder moves are metadata only: the session keeps its transcript, its model, and
any running turn. Archived (history) sessions cannot be moved — bring one back
with `session_revive` first. There is no delete verb here.

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

## Board columns

The sidebar board shows one column per tag filter (or live-state lane). The
column list is part of the person's own layout, shared by every session.

| Tool | Arguments | What it does |
|---|---|---|
| `chat_tag_column_list` | none | Every column in board order: id, name, what it filters on. Read-only |
| `chat_tag_column_create` | `name` (required), `tag` (required) | Append a column showing sessions that carry `tag`. A column with the same name and tag is returned instead of duplicated |
| `chat_tag_column_move` | `column` (required), exactly one of `before` / `after` | Move one column next to another. The others keep their order |

`tag`, `column`, `before` and `after` take an id or an exact name. There is
no delete or retag verb: removing a column or changing its filter removes a view
the person built. An app agent and a crew member can list columns but not
write them; the `/api/chat/tag-columns` write endpoints refuse both.

## Pins

| Tool | Arguments | What it does |
|---|---|---|
| `chat_session_pin` | `session` (required), `pinned` (required boolean) | Pin (`true`) or unpin (`false`) a live session |

`session` takes the same slot key, `dashboard:<slot>` key or unique exact title
as `chat_folder_move_session`. Asking for the state the session already has
writes nothing and says so. `chat_folder_tree` marks pinned sessions
`[pinned]`. An app agent pins only its own sessions and a crew member only a
session it owns or created; the `PATCH /api/chat/slots/<slot>/pin` endpoint
enforces both. Archived sessions cannot be pinned.

## What you cannot reach

Session control authorizes on the **calling session's identity**, and only a
gateway-issued key counts. Refusals you should expect, by code:

| Code | Meaning |
|---|---|
| `target_not_found` | No open session matches that key or title. A closed tab is out of scope for every verb except session_revive, whose target is precisely an archived session |
| `target_already_live` | session_revive only: the session is open already. The message carries its live key — address it directly |
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
| `target_busy` | Model change and reload: the target has a turn, queued messages (reload) or sub-agents in flight, so nothing was changed |
| `target_changed_during_reload` | Reload: the target left your reach while its process was being reset. The process was reset; no reload notice was written |
| `reload_failed` | Reload: the teardown failed before the target's process was removed. Nothing was torn down |
| `invalid_broadcast_mode` | `session_broadcast` needs `mode` to be `queue` or `steer`; there is no default |
| `caller_changed_mid_broadcast` | The calling session moved workspace while a broadcast was in flight, so its per-target report is withheld. The deliveries already happened — do not re-send |
| `too_many_targets` | A broadcast reaches at most 50 sessions. Refused, never truncated — name a subset |
| `target_required` | A broadcast was given a target list that names no session. Omit the list to reach everything you created |
| `delivery_timeout` | One target exceeded its per-delivery bound, so the broadcast cancelled that call and continued. The row reflects the exact text's observed state: pending or queued means it may still run and must not be re-sent; absent from both means the hand-over was not reached and re-sending is safe; an unavailable original slot means the outcome is unknown and the row advises neither action. No case claims delivery or certain execution |

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

A **channel agent** (Slack, Telegram, and the rest) is blocked from every
session tool by `CHANNEL_AGENT_BLOCKED_TOOLS` in `src/kiro_crew/channel.py`.
Reading a dashboard transcript would pull a private conversation into a channel
other humans can see, and sending would run channel text as a turn inside it.
`chat_session_pin` is blocked there too, because it rearranges the person's
sidebar the same way `session_adopt` and `session_release` do. The tool also
refuses a `channel:` caller itself, before listing any session, so an
auto-approved call that never reaches the permission prompt is still refused.
`chat_tag_column_create` and `chat_tag_column_move` are blocked and refused at
dispatch in the same way, because they rearrange the person's board.
`session_broadcast` is blocked for that reason multiplied by the fleet, and
`session_status` because its rows carry other sessions' titles — the names of the
user's private work, in front of whoever is in the thread.
`chat_folder_steering_set` is on the same list too: it names host directories the
gateway reads into every later chat in the folder, on words from a thread other
people are in.

### Switches and ceilings

| Knob | Default | Effect |
|---|---|---|
| `agent.session_control` | `true` | The whole surface. Turn it off to withdraw the capability from every agent at once without editing a spec |
| `agent.member_dispatch` | `true` | A crew member's DM session drives workers it created even when session control is off. Turn it off to put member callers back under the switch |
| `agent.crew_panel` | `true` | A crew member publishes its own webview, shown in that member's drawer on the Crew page. Its own mount and its own switch, so withdrawing session control leaves the drawer alone and withdrawing the drawer leaves session control alone |

Rate limits are per caller, per verb, over a 300-second window: **20** session
creates (forks included), **10** folder creates, **10** tag creates, **10** board-column creates. Capacity ceilings sit behind
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

- [Subagents & parallel work](subagents.md) — `spawn_run` and when to delegate
- [Dashboard](dashboard.md) — the sidebar, tabs and queued messages these tools write to
- [Monitor loops](monitor-loops.md) — the `wait`-then-read shape a watcher uses
- [Configuration](configuration.md) — where `agent.session_control` lives
