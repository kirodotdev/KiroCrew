# Mastermind

Owners: `kiro_crew.mastermind` (the tree, its store and summarizer, the gateway
service and the slot leaf), `kiro_crew.dashboard.handlers.mastermind` (the memory
routes), `kiro_crew.mcp_tools.mastermind` (the `mastermind_zoom` and
`mastermind_date` tools), and the three turn hooks in the dashboard runner listed in
[Turn hooks](#turn-hooks).

## 1. Purpose

Mastermind is one dashboard chat whose memory never fills up. Every message is
kept, and every message and pair of adjacent summaries is summarized into a
binary tree of one-line summaries. Each turn sees a *view* of the whole chat
(recent messages one line each, older ones covered by fewer, broader lines) and
runs on a fresh backend conversation, so the view is the turn's only memory and
no compaction ever drops anything. The agent opens any line back into the two it
was made from with `mastermind_zoom`, down to the original message.

The design follows UniiChat's memory tree (Apache-2.0).

## 2. The session and the switch

The Mastermind session is the dashboard slot whose key is `mastermind`
(`mastermind.slot.MASTERMIND_SLOT_KEY`). Its turns run as Mastermind only while
Mastermind is on: `<data home>/mastermind/enabled` exists. `slot.enabled` reads
that file once per gateway and keeps the answer on its state; only
`POST /api/mastermind/enabled` changes it. Turning it on makes and pins the session
(title `Mastermind`) if it is missing; turning it off keeps the session and its
memory, which then behaves as an ordinary session.

While Mastermind is on, its session cannot be closed through the slot API
(`DELETE /api/chat/slots/mastermind`) or session control (`close_target`): both
refuse with `409 mastermind_protected`. It is removed only by
`POST /api/mastermind/remove`, which turns Mastermind off first. Idle cleanup
already keeps it, as a pinned session.

The dashboard reveals Settings > Mastermind behind the `mc-preview-mastermind`
preview flag.

`mastermind/slot.py` is a leaf the runner imports on every turn; it does not load
the tree.

## 3. The tree

`mastermind/tree.py` holds one chat's tree under `<data home>/mastermind/`.

- Node `(0, i)` is message `i` in at most `NODE_BYTES`; node `(lv, i)` merges
  `(lv-1, 2i)` and `(lv-1, 2i+1)`. A source that fits is its own node and is
  stored at once; a longer one is a job for the summarizer.
- A view lists nodes tiling the chat from message 0 to its end. Past its `high`
  bytes it merges the adjacent siblings whose parent is built and most due
  (`(T + 1) / 2^lv - i`, T the view's end) down to half, so detail fades with age
  and the view changes only at its end between merges. The turn view's high is
  `view_high_for(window)`: `VIEW_WINDOW_SHARE` of the turn model's context window
  (`window_for_provider_client`, `DEFAULT_WINDOW_TOKENS` when unknown) at
  `BYTES_PER_TOKEN`, between `VIEW_HIGH_BYTES` and `VIEW_MAX_BYTES`. A quarter
  leaves the turn its working room under the session's autocompact threshold. A
  smaller high merges the view down at once; a larger one lets it grow, never
  un-merging. The rendered view is capped at the same high: while summaries are
  pending it keeps its newest lines and names the left-out span's roots to zoom.
  The summaries read their own view (`seen`, `SEEN_HIGH_BYTES`) up to the first
  message not built.
- A line not built yet renders as `(not summarized yet: zoom it)`: a turn never
  waits for a summary. The turn also gets the last `RECENT_MESSAGES` messages
  whole in `<recent>` (within `RECENT_BYTES`), so the previous reply is always in
  view whether or not it is summarized yet.
- A merge starts when a view passes its high and lands whole; one held up by
  missing parents goes on at the next message, never at a summary landing.
- Storage (`mastermind/store.py`): `messages.jsonl` and `nodes.jsonl` are
  append-only and read by byte offset; `views.json` is derived and replaced
  whole. A load cuts each log back to its last whole record before anything is
  appended after it (the messages log at its first torn, unreadable or
  out-of-order record), and drops every node that reaches past the messages kept,
  so a torn write loses that one record and nothing after it.
- Threading (`mastermind/worker.py`): the tree is not thread-safe and does file
  I/O, so every read and write of it runs on one dedicated worker thread, never on
  the gateway's event loop.

## 4. Summaries

`mastermind/summarizer.py` keeps up to `MAX_PARALLEL_SUMMARIES` jobs running.
Each is one tool-free call through `llm_helpers.run_bg_oneliner` (a fresh
background session on the selected backend) on the model of
`agent.role_models.mastermind` (`service.summary_model`; unpinned, `"auto"`, never the
chat's model). A line over `NODE_BYTES` is asked
for again, showing where it must end, up to `SUMMARY_TRIES` times, and the
shortest is kept; a reply that is empty is replaced by the clipped source. A call
that fails gives its job back to the tree and the summarizer retries after
`SUMMARY_RETRY_SECS`, so an outage only delays a node.

## 5. Turn hooks

| Where | What |
|---|---|
| `chat_runner._run_chat`, before notices are prepended to the message | `service.turn_prefix` renders the view and `<recent>` into the request prefix (`request_prefix_context`, so the user's span is unshifted) and remembers the turn's own message for the landing; a slash command drops a remembered message instead |
| `chat_turn/prompt_assembly._session_replay_history` | a Mastermind cold start replays no transcript; it pays the replay-suppression flag and the first-turn history debt like the suppression arm |
| `chat_runner`, beside `post_turn_reply` on a landed turn | `service.turn_landed` writes the message and the turn's spoken reply rows (`_turn_reply_mids`) to the tree in the background and marks the turn landed |
| `chat_turn/queue_cycle._end_turn_tail`, before the deferred-reset consume | `slot.turn_ended` arms `_pending_discard_conversation_key` for a landed turn, so the same consume discards the backend conversation (`replay=False`) |

- **Recovery.** A turn that does not land (stopped, or continued by a recovery)
  keeps its conversation and its remembered message. A synthetic turn (a
  recovery, a synthesis) does not replace the message, so the continuation lands
  the original request once.
- **Kinds.** A message automation wrote (a cron notification, a subagent report,
  a monitor wake, an auto-nudge) is recorded as `event`, never as `user`.
- **Privacy.** An incognito or temporary slot's turns are never written to the
  tree, and a temporary slot is not given the view.

## 6. Routes and tools

| Route | Caller | Answers |
|---|---|---|
| `GET /api/mastermind/zoom?id=&n=&at=` | `mastermind_zoom` (MCP-only, strict internal) | the session the Mastermind slot runs on, while Mastermind is on |
| `GET /api/mastermind/date?id=` | `mastermind_date` (MCP-only, strict internal) | the same |
| `GET /api/mastermind` (and `/status`) | Settings, sidebar | the switch, whether the session exists, the tree's size and pending summaries |
| `POST /api/mastermind/enabled {enabled}` | Settings | turns Mastermind on (making and pinning its session) or off |
| `GET /api/mastermind/view` | Settings memory browser | the view's lines |
| `GET /api/mastermind/browse?id=&n=&at=` | Settings memory browser | a line opened, or a message whole |
| `GET /api/mastermind/export` | Settings | every message and node as a JSON download |
| `POST /api/mastermind/clear` | Settings | forgets every message and summary; the transcript stays |
| `POST /api/mastermind/remove {forget}` | Settings | turns Mastermind off and closes its session; `forget` also clears the tree |

The memory routes recognize the caller with `_recognize_session` and refuse
anything but the effective session key of the Mastermind slot
(`not_mastermind_session`). The Settings routes answer the dashboard owner alone
(`require_owner_dashboard_request`). The tools are stateless, pass the strictly
verified session key (`mcp_tools/mastermind.py` is a reflexive tool module), and
are listed only while Mastermind is on (`advertised`); each Mastermind turn starts
a fresh session, so a switch reaches the next turn's tool list.
