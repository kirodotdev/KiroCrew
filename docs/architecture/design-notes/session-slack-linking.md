# Session and Slack Thread Linking

How a Slack thread maps onto a Kiro Crew session, and how messages mirror in both
directions across the two surfaces.

The invariant the whole design serves: **one conversation, one kiro-cli session,
two surfaces.** A linked thread and its dashboard tab drive the same session key,
so neither surface spawns a second ACP process, and neither has a private
transcript.

## Where the link lives

The link is persisted on the session map entry (`session_map.py`,
`~/.kiro/crew/session_map.json`), not in a gateway-lifetime dict, so it survives a
restart. Two fields on the entry:

```
slack_thread_ts    Slack thread parent timestamp
slack_channel_id   the channel the thread lives in
```

`SessionMap` keeps a `_thread_to_session` reverse index (thread_ts -> session key)
rebuilt from `_data` on load and maintained by `set_slack_link` /
`clear_slack_link` / `_remove_entry`. `SessionManager` forwards
`set_slack_link` / `get_slack_link` / `clear_slack_link` /
`get_session_for_thread` through to it.

Entries are also read as a legacy plain string (`{"key": "sid"}`) and migrated to
the dict shape on load, and bare Slack `thread_ts` keys are namespaced to
`slack:<thread>` via `messaging.link.canonical_key`. The raw `thread_ts` stays
inside the entry so the reverse index and the resume path are unaffected by that
rename.

`set_channel` / `set_thread` / `get_channel` / `get_thread` on `SessionManager`
remain as thin compatibility shims over `set_slack_link` / `get_slack_link`; new
code uses the link API directly.

### Channel-neutral generalization

`set_mirror_link` / `get_mirror_link` / `clear_mirror_link` expose the same
binding as a channel-neutral `ChannelLink` so the dashboard turn path can deliver
a reply to any proactive-capable channel through `Transport.send_message` without
special-casing Slack. Slack is the one channel that routes back through the
dedicated fields and the `_thread_to_session` index; every other channel stores a
`ChannelLink` under `mirror`. A legacy Slack-only entry synthesizes the equivalent
Slack `ChannelLink` on read, so callers never branch.

`mirror_accepts_inbound` distinguishes a two-way session-resume binding from an
outbound-only mirror. Slack never sets it, because Slack has its own inbound leg.

## In-memory projection on the dashboard side

`_ChatSlot` mirrors the persisted link into `_slack_linked`, `_slack_channel`,
`_slack_thread_ts` (`dashboard/state.py`), serialized to the frontend as
`slack_linked` / `slack_channel` / `slack_thread_ts`. `DashboardState` keeps
`_slack_to_slot` (thread_ts -> slot name) for the inbound Slack lookup.

Both are caches over the session map, not the source of truth:

- `get_or_create_slot` hydrates the three slot fields from
  `sessions.get_slack_link(effective_session_key(slot))`, so a restored tab
  re-adopts its link with no separate restore pass.
- Hydration goes through `_is_genuine_slack_link`, which requires BOTH a
  thread_ts and a channel id and rejects a channel id namespaced to a non-Slack
  transport. Other transports still write their namespaced origin id through the
  legacy channel field, and those are projected separately under `links`. Without
  this check a Telegram-origin session would light up the destructive Slack
  actions in the UI.
- `get_linked_slot` self-heals: if the mapped slot is gone, is no longer
  `_slack_linked`, or now points at a different thread, the stale
  `_slack_to_slot` row is dropped and the lookup misses.
- `link_slack` is the only writer of a `_slack_to_slot` row. It evicts the old
  thread row when a slot re-links, and when the target thread was owned by a
  *different* slot it clears that slot's fields and its persisted link too, so a
  thread can never read as owned by two slots.

Note the asymmetry in what survives a restart. The persisted link (and therefore
the outbound dashboard-to-Slack mirror, which reads `get_slack_link` per turn)
comes back automatically. `_slack_to_slot` does not: nothing repopulates it at
boot, so the inbound `get_linked_slot` fast path only exists for links formed in
the current process. Inbound Slack messages still reach the right session after a
restart via `sessions.get_session_for_thread(reply_ts)` in `handle_message`, which
reads the persisted reverse index.

`effective_session_key(slot)` is the authoritative key throughout: a
channel-born slot carries the real channel key in `linked_session_key` and its
turns run on the channel session, so the link has to live there. Deriving the key
from the slot NAME instead would write `dashboard:slack:<ts>` and leave the real
link untouched, so mirroring would silently resume on the next turn.

## Establishing a link

| Origin | How the link is set |
|---|---|
| Slack DM or @mention thread | Self-link. `handle_message` calls `set_slack_link(session_key, reply_ts, channel)` on a new session. `reply_ts` (`thread_ts or msg_ts`), never the namespaced key, is stored as `slack_thread_ts`: storing the namespaced form would corrupt reply routing. |
| Dashboard, user action | `POST /api/chat/slots/{name}/slack-link`. Opens a DM (or uses a supplied channel), posts a thread anchor, links, and back-fills the last 5 messages as context. |
| Dashboard, auto-link from a redirect | The same endpoint with `thread_ts` in the body. Links to THAT existing thread instead of posting a new one, which is what makes a thread reply route back bidirectionally. Context back-fill is skipped, since the thread already contains those messages. |
| Slack thread imported to dashboard | `!link-to-dashboard` (`/kirocrew link-to-dashboard`) fetches the thread, redacts each message, imports up to the last 50 into a fresh slot, then `link_slack`. Idempotent: an already-linked thread returns its existing slot. |
| `/kirocrew sessions` resume | Posts a resume header in-thread or in a DM, then `set_slack_link` plus `dashboard_state.link_slack`. |

The anchor message title never exposes a raw slot key. The chain is LLM title,
then a one-line snippet of the first user prompt, then a neutral default;
redaction (`redact_and_truncate`) runs on the full text BEFORE truncation so a
truncation boundary cannot split and thereby hide a credential.

## Message routing

### Slack to dashboard

`maybe_route_linked_thread` (`slack/handler.py`) runs before hook handling and
before any turn work, on both the native and messaging-transport paths:

1. Look up `get_linked_slot(reply_ts)`. The index is keyed by the **bare
   thread_ts**, not the namespaced session key, so canonical `slack:<ts>` keys
   still hit.
2. Authorize FIRST. An unauthorized user is denied and SEL-logged before
   anything is appended to the slot.
3. `!`-bang commands deliberately fall through to normal Slack handling.
4. Append the user message to the slot (redacted for display only, the LLM
   receives the original text), broadcast it over the WebSocket, and either
   start `_run_chat` or `queue_append` if the slot is already running.

`handle_message` also consults `sessions.get_session_for_thread(reply_ts)`
independently: when a thread resolves to a different session key, the turn is
re-pointed at that key so the reply runs on the linked session. Channel
activation checks (`observe`, `mention`, `review` with `thread_follow`) treat a
thread with a mapped session as an active thread, so a follow-up reply does not
need a fresh @mention.

### Dashboard to Slack

In `chat_runner._run_chat`, gated on `not is_slash and not _is_synthetic`:

1. Read `get_slack_link(session_key)`. A dashboard session may hold its link on
   the bare key while the turn runs under the `dashboard:`-prefixed one, so the
   runner copies the link forward at turn start. Both spellings must be cleared
   on unlink or the next turn re-inherits the link.
2. Echo the user message to the thread, then `start_stream` for live tool
   animations.
3. Tool calls mirror as stream tasks (`append_task` in_progress, then complete),
   with titles redacted and capped.
4. The completed reply is converted to Slack mrkdwn, redacted, split on the
   channel message limit, and posted; extracted OPTIONS render as Block Kit
   buttons.
5. `stop_stream` runs in the turn's `finally` so a cancelled or failed turn does
   not leave a live stream.

The channel-neutral leg (`_deliver_cross_surface_user_message` /
`_deliver_cross_surface_reply`) handles a linked NON-Slack channel and is skipped
for Slack, which keeps its dedicated rich streaming mirror. It is
capability-gated on `supports_proactive_send` and governance-gated fail-closed.

### Tool approvals

A tool-approval prompt on a linked session is mirrored into the thread with
Approve / Reject buttons (`post_linked_approval`), because a Slack-only user
would otherwise never see it and the turn would park on the 2h timeout holding
the slot lock. The click resolves the dashboard slot's approval future, so there
is still exactly one caller of `approve_tool` / `reject_tool`.

Delivery failure is not allowed to become a silent park: if the post fails (or
anything raises before the future is resolved, including `CancelledError` on the
cancel path) the future is resolved as `rejected` and the user is told, so they
retry rather than wait. A cancelled turn never obtained consent, so `rejected` is
the correct reading.

`is_dm=False` is passed deliberately: trust for a linked slot is a dashboard-side
mode and is not wired through this path, so only Approve and Reject are offered.

## Pausing (and picking back up)

The dashboard never says "pause", and it says nothing per-channel either. EVERY
channel is ONE menu row whose LABEL IS THE ACTION — `Connect to X` when it is not
connected, `Disconnect from X` when it is — and clicking it toggles. Two states,
for Slack and Discord and every future transport alike, rendered from one flat
row list with no per-channel branches in `LinkedSurfacesSection.tsx`.

What that deleted, rather than relabelled: the `Connected: X` status line, the
role badge (`Origin` / `Mirror` / `Two-way`), the `Offline` badge, the reminder
item, both destructive verbs (`Release X` / `Stop mirroring to X`) and their
confirms — and the nineteen i18n keys they carried, replaced by one interpolating
`connect_to` / `disconnect_from` pair. No tooltip explains that the binding is
retained or that replying resumes it; those are treated as givens. The row is a
plain menu item, NOT a `menuitemcheckbox`: an action label and an `aria-checked`
state contradict each other, and the verb already tells the user where they are.

An `origin` link gets no row at all. A session's BIRTH conversation is not a
binding the dashboard can toggle, and `channelOrigin.ts` already marks it on the
sidebar from the slot key.

A session can be connected to several channels at once, so there is one row PER
binding, each with its own state and its own channel-scoped calls: muting Telegram
leaves Discord connected. A channel already bound is not offered a second time —
one binding per channel type. The single exception to "no confirm" is taking a
conversation another session holds: that disconnects the other session, so it asks
first (see "One session per conversation" below).

Disconnect maps to `slack-pause` / `mirror-pause`, NEVER to either unlink, because
the retained binding is what makes the row reversible: a muted channel and one
that was never connected render identically, and the click that connects either
one resumes the muted binding rather than minting a second. Both unlink endpoints
survive with no menu caller; the one behaviour they still uniquely offer is
releasing a conversation so a later reply starts a FRESH session.

Two release affordances DO remain outside the menu, and they are the same idea in
two places: the in-channel `!unlink` command, and `InboundLinkChip`'s Release
button (its own docstring calls itself "the dashboard-side equivalent of the
channel's `!unlink`"). Both exist because a non-Slack conversation hosts exactly
one session and `set_mirror_link` has no occupancy check, so severing is the only
way to hand a conversation to a different session. Evicting on connect would make
both redundant — see issue #1887.

Unlink is a release; pause is a mute. `POST /api/chat/slots/{slot}/slack-pause`
sets a `slack_paused` presence flag on the session-map entry and changes nothing
else: `slack_thread_ts`, `slack_channel_id` and the `_thread_to_session` row all
stay. That asymmetry is the whole design.

- **Inbound is untouched.** `_resolve_thread_owner` still resolves the thread to
  its owning session, and the unclaimed-thread guard still sees the thread as
  claimed, so a reply lands in the original conversation instead of forking a new
  `slack:<ts>` session. A reply is also the resume signal: `_resume_if_paused`
  lifts the flag on the way through, on both the transport and the native path.
- **Outbound consults the flag.** `chat_utils.slack_mirror_is_paused` gates turn
  mirroring — the user-message echo (which is what also silences the tool stream,
  the assistant reply and the stream teardown, since they all key off it), the
  compaction notice, the auth-error mirror, and the linked approval prompt.
- **Deliveries that merely address the thread are NOT gated** — a cron result, a
  subagent completion, a requested file, an auto-nudge tick. Those fall back to
  the owner's DM on an empty link, and the nudge path deletes the loop outright,
  so reading paused as no-link there would reroute messages the user asked for
  and destroy a live monitor.

`_slack_linked` deliberately stays `true` while paused: `get_linked_slot` evicts
its `_slack_to_slot` entry when that boolean is false, so expressing pause by
clearing it would make the slot self-purge and a resumed turn would never render
in the open tab. Pause touches no slot state at all — the session map is the
single source of truth, which is why there is no fourth slot field.

Reconnecting re-issues `slack-link`. Its already-linked branch detects the paused
link, lifts the flag, re-binds through `link_slack`, and re-seeds the existing
thread with history. That re-seed is the deliberate exception to the "only seed a
NEW thread" rule, because the thread has a gap in it precisely because mirroring
was off. Reply-to-resume does not re-seed: the user is reading the thread already.

A Slack-BORN session is what made this reachable. Its self-link used to be
suppressed from the serialized `links` array entirely, so the dashboard saw no
Slack link, fell through to the target picker, and offered "Send to Slack" for a
conversation already in Slack — with nowhere to hang the control. The link is
now emitted with `direction: "origin"`, which keeps it out of every mirror-shaped
affordance while making it addressable. `slack_linked` stays `false` for those
sessions so the frontend does not also synthesize its phantom mirror row.

### The channel-neutral half

A session can hold SEVERAL non-Slack bindings — one per channel type, stored as
`mirrors: {channel_type: binding}` with `accepts_inbound` and `paused` living ON
each binding. One per type is the product rule as well as a storage convenience:
a conversation hosts one session, so a session holding two of the same channel
could not be addressed unambiguously either. A legacy single-`mirror` row (plus
its entry-level flags) is folded into that shape by one normalizer — read-compat,
never migrating on read, because a read path that writes turns every listing into
a disk mutation. Writing is the migration point and retires the legacy keys.

`mirror_paused` is the non-Slack twin of the Slack mute, and the differences are
all in Slack's favour being unnecessary rather than the reverse:

- **Storage** refuses to create a binding or an entry, so a mute can never
  outlive what it describes; both clear paths and a rebind drop it.
- **Egress** resolves EVERY binding (`_resolve_mirror_targets`), governs each
  through the same ladder, mute-checks each on its own, and splits at each
  transport's own length limit. One channel denied, unregistered, unable to send
  proactively, or failing mid-send does not cost the others their message. The
  unnamed `is_mirror_paused` therefore means "every binding is muted", never
  "any" — that reading is what stops one muted channel silencing a sibling.
- **There is no approval leg and no auto-nudge hazard here.** The loop's deletion
  path reads the dedicated Slack fields only (`get_channel`) and `binding_key_for`
  never emits a mirror key, so a `mirror` entry is invisible to it; and a
  tree-wide sweep finds no approval delivery to a non-Slack link at all. A
  disconnected channel really does go quiet.
- **Reconnect** re-issues `mirror-link` naming the channel, shares one
  `_deliver_catch_up` helper with link creation, and re-asserts `accepts_inbound`.

### One session per conversation

Inbound is where Slack and the rest differ structurally. Slack routes a thread
reply through its single-valued `_thread_to_session` index, so a thread has
exactly one owner. A non-Slack conversation has no threads to scope bindings to —
a Discord DM cannot hold them at all — and `find_mirror_sessions` returns a LIST,
with the inbound resolver refusing to pick: two inbound bindings on one
conversation route a message to NOBODY (`session_resume.py` logs
`ambiguous inbound bindings; routing denied`).

So the dashboard's connect enforces the occupancy rule that makes inbound safe.
It sets `accepts_inbound` — without which a reply resolves no owner and falls
through to the conversation's own session key, the defect where connecting a
session and then replying in Discord landed in a brand-new tab — and it refuses a
conversation another session holds with `409 conversation_occupied` until the
caller passes `confirm`. The check runs BEFORE any side effect, so an unconfirmed
attempt announces nothing, evicts nothing and binds nothing. A confirmed one
clears the previous binding by location and posts a line into the conversation,
because whoever is reading there needs to know which session they are talking to.

That rule is per CONVERSATION, not per account: two sessions may each hold a
different Discord conversation. It generalises unchanged when the app is added to
a channel, since a channel is just another conversation id — and Discord threads,
which exist in channels but not DMs, would let a conversation host several
bindings. Neither is built: the transport has no create-thread call, and
`discord/transport.py` refuses inbound from any thread outside its configured
allowlist.

## Unlinking

`POST /api/chat/slots/{slot}/slack-unlink` is the symmetric counterpart. It
clears the link (both the effective key and, for a dashboard session, its bare
twin), resets the three slot fields, and posts a best-effort courtesy note into
the thread so a Slack watcher knows why it went quiet. The session, its history,
and the thread itself all survive. Idempotent: unlinking an unlinked session
returns `was_linked: false`.

`clear_slack_link` evicts the `_thread_to_session` row as well as the fields.
Without that eviction a later reply in the old thread would re-route to the
session and silently re-engage mirroring.

Auth posture matches the link endpoint and adds no new surface: both are
mixed-internal via the `/api/chat` prefix, so on loopback they accept the
internal secret and otherwise fall back to dashboard-token plus CSRF. Neither
belongs in the strict `internal_paths` set, which would wrongly restrict a
browser action to loopback-only callers.

## Concurrency and ordering

- **Two surfaces, one semaphore.** Dashboard and Slack both acquire the same
  per-session `Semaphore(1)`, which is the mechanism that keeps them on one
  conversation. A slow turn on one surface blocks the other; that is the same
  serialization concurrent Slack messages in a thread already rely on.
- **No duplicate processes.** The race check inside `get_or_create` resolves a
  simultaneous acquire for one key: the loser shuts down its provider and uses
  the winner's.
- **No cross-surface ordering guarantee.** Slack events arrive asynchronously
  while slot appends are synchronous, so interleaving between the two surfaces is
  not ordered. Within one session the semaphore still serializes turns.
- **Mirror posts are best-effort and never buffered.** Every mirror call site
  wraps its post in a `try`/`except` that logs at debug and continues, so a
  Slack-side failure (rate limit, transient 5xx) costs one mirrored message and
  not the turn. Only `open_dm` retries (`slack/retry.py`); `post_message` stays
  single-shot at each call site so a retry cannot duplicate a message.
