# WhatsApp integration

Chat with Kiro Crew on WhatsApp from your own account: scan a QR code once and
the gateway joins your WhatsApp as a **linked device** — the same mechanism as
WhatsApp Web. There is no bot account and no Meta Business API: messages the
agent sends come from *your* number, which is what makes the channel powerful
(message yourself as a command line, have the agent send reminders to friends,
lend a hand in your groups) and also what makes its safety rules strict.

Requires the optional dependency extra:

```bash
pip install 'kirocrew[whatsapp]'
```

## Risks — read before enabling

This channel speaks the **unofficial WhatsApp Web protocol** (via
[neonize](https://github.com/krypton-byte/neonize)/whatsmeow). Automating a
personal account violates WhatsApp's Terms of Service, and WhatsApp does ban
numbers for abusive automation. Personal-scale use (your own chats, a few
groups, no broadcast/marketing) has a long community track record, but the
risk is never zero — do not link a number you cannot afford to lose. The
channel deliberately ships **no bulk-send affordances**, replies only where
configuration allows, and rate-limits unprompted group replies.

## Setup

1. Install the extra (above) and restart the gateway.
2. Enable the channel in `~/.kiro/crew/config.json`:

   ```json
   { "whatsapp": { "enabled": true } }
   ```

   Or run `kirocrew setup --whatsapp`, which reports whether the optional extra
   is installed, reports whether a paired session store already exists, and sets
   `whatsapp.enabled` for you. It collects no credentials: there is no token, and
   pairing still happens in step 3 below.

3. Open **Settings → Channels → WhatsApp**. With the channel enabled and no
   session on disk, the gateway begins pairing as it starts the channel and holds
   a rotating code. Click **Show pairing code** to see it, then scan with your
   phone: WhatsApp, Settings, Linked devices, Link a device. The code rotates
   every ~20 seconds and the panel follows it until the scan lands.
4. The badge flips to **Connected**. Pairing state persists in
   `~/.kiro/crew/whatsapp/session.db`, so you scan once rather than per restart.

### Pairing starts with the channel, not with the button

The rotating code is produced inside the channel's own connect, so no dashboard
request can begin a pairing session. **Show pairing code** reveals a code the
gateway already holds; it is disabled when there is none, and the panel names the
action that would produce one instead of appearing to offer pairing it cannot
start. Re-pairing is therefore a three-step sequence: unlink, restart the
gateway, scan the new code.

### Unlinking

**Unlink this device** is the in-product revoke. It is local only: the endpoint
refuses a request that did not come from the machine running the gateway, so a
remote dashboard session does not show the control at all. The first click arms
it and the second performs it, because a linked device carries no second factor
and no expiry.

Three outcomes, and the panel keeps them apart because two of them leave work to
do:

| Outcome | What is true afterwards |
|---|---|
| Unlinked | The device is revoked and the local session file is gone. Restart the gateway to pair again. |
| Unlinked, session file kept | The device is revoked, but the local file holding its keys could not be deleted. Remove it under the data home before pairing again. |
| Refused | WhatsApp rejected the logout, so the device is **still linked** and the session was kept deliberately, because it is the only credential that can retry. Try again, or revoke the device from your phone under Linked devices. |

A refused unlink is never reported as success. Deleting the local session after a
failed logout would leave the device live on your account with the only credential
that could revoke it gone. If the channel is not running there is nothing for the
endpoint to unlink from and it says so; your phone's **Linked devices** list is the
out-of-band revoke that always works.

### What the badge means

The badge reads the pairing lifecycle rather than a "configured" flag, so a session
that exists and is carrying nothing is visible instead of being reported as
connected:

| Badge | Meaning |
|---|---|
| Connected | Paired and carrying traffic. |
| Waiting to be paired | The gateway is holding a rotating code. |
| Paired, but not connected | A session exists and the channel is not carrying traffic. |
| The link was revoked | Your phone, or WhatsApp, unlinked the device. Restart the gateway to pair again. |
| Blocked by WhatsApp | A temporary ban on the number. |
| Not paired | No session, or the channel is not running so the gateway has no client to ask. |

Whenever the channel is down and the gateway recorded a reason, that reason is
printed under the badge verbatim: a missing dependency extra, a refused pairing, a
dropped socket. The panel answers "why", not only "whether".

## The self-chat is the command line

Open your own chat (WhatsApp's "Message yourself") and type. The agent
answers there, with your full session context — and because it is *your*
account, it can act on any other chat from that vantage point: "summarize
what I missed in the family group", "send Alex the meeting link at 5pm".

Echo discipline: the agent tracks the IDs of messages it sends, so its own
replies (which arrive on the same account) are never mistaken for your
commands, and anything **you** type is never mistaken for an echo.

## Access policy

`whatsapp.dm_policy` controls who may command the agent in direct chats:

| Policy | Meaning |
|---|---|
| `self` (default) | Only the linked account itself — your own messages. |
| `allowlist` | You, plus numbers in `whatsapp.allowed_wa_ids`. |
| `open` | Any DM sender (not recommended). |
| `disabled` | No direct chats. |

Unknown values deny everyone (fail closed). Denials are SEL-audited. Senders
other than you never get tool-approval or session-steering affordances,
whatever the policy, and their turns are answered without your memory,
lessons, skills or conversation history in the prompt, so an admitted peer
cannot be told what you have been working on.

## Groups

Groups are **opt-in via `whatsapp.groups`** — the agent ignores any group not
listed. Each entry:

```json
{
  "jid": "120363012345678901@g.us",
  "name": "Family",
  "mode": "mention",
  "rules": "",
  "cooldown_s": 120
}
```

- `mode: "mention"` — replies only when @-mentioned or when someone replies
  directly to one of the agent's messages.
- `mode: "rules"` — additionally lets the agent speak unprompted when the
  free-text `rules` clearly apply ("answer 3D-printing questions"; "help with
  homework requests"). The model is instructed to stay silent otherwise (a
  sentinel reply is discarded before it reaches the group), and `cooldown_s`
  caps unprompted replies per group regardless.
- `mode: "off"` — keep the entry, mute the group.

Groups are configured from **Settings → Channels → WhatsApp → Groups** rather
than by hand. The picker lists the groups the linked account has joined, which the
gateway can only report while the channel is connected, and each row carries that
group's mode, its free-text rules and its cooldown. A group is added at `mention`,
the mode that never speaks unprompted, so adding one cannot by itself put the agent
into a conversation. Editing `whatsapp.groups` in `config.json` is still
equivalent: it is what the panel writes. You can also ask the agent in your
self-chat, "list my WhatsApp groups".

A JID that is mistyped, or a group you have since left, is silently ignored rather
than reported as an error, because an unconfigured group and an unmatched one look
the same to the gate. The gateway therefore checks the configured JIDs against your
actual group list once, on connect, and logs a single warning naming any that do
not match. Re-pick those from the group picker.

## Reminders and outbound messages

The channel supports proactive sends to any chat (it is your account — there
is no bot messaging window). Cron jobs and the `send_message` tool can
deliver to WhatsApp targets, so "remind Priya about dinner at 6" becomes a
scheduled WhatsApp message from you. Outbound goes through the same
personal-scale rate limiting as replies.

## Commands

| Command | Effect |
|---|---|
| `/new` | Start a fresh session (new context). |
| `/compact` | Compact the current session's context now, and report the result. |
| `/status` | Runtime summary (sessions, uptime, counters). |
| `/stop` | Interrupt the running turn, or clear the queue if nothing is running. |
| `/help` | List the commands. |

`/help` is answerable by anyone the access policy admits, because it discloses
only the command list. Everything that acts on the session is **operator only**:
from anyone else those words are treated as plain text. Matching is whole-message
and exact, so `/stop the presses` reaches the agent as a sentence.

## Behaviour notes

- **The reply streams**: it appears as soon as there is something to read and
  is then edited in place as the agent continues, because the WhatsApp Web
  protocol allows editing a sent message (the Business API does not). Edits are
  throttled rather than per word: each one is a real send, and a personal
  account that sends too fast gets rate-limited. A long answer arrives as
  several messages split at paragraph and code boundaries (4096 characters each),
  and once WhatsApp's 20-minute edit window closes the reply continues in a new
  message instead.
- **Tool approval arrives as a numbered question**: the agent does not use tappable
  buttons here, so it asks and you reply `1` (approve), `2` (deny) or
  `3` (trust this session). No reply within five minutes denies. Only the linked
  account can answer, so an allow-listed peer or a group member cannot approve
  anything.
- **Photos, voice notes and documents work in both directions**: send one and the
  agent reads it (a voice note is transcribed); when the agent produces an image
  it arrives as a picture rather than a file path. Anything that cannot be read
  (a location, a poll, a contact card) is acknowledged rather than ignored.
- **Commands**: `/help` lists them, and `/new`, `/compact`, `/status` and `/stop`
  do what they say. Only the linked account can run anything except `/help`.
  `/compact` compacts on the spot and answers with the result, so a reply saying
  the context was compacted means it has been.
- **Context is watched at the end of every turn**: past
  `whatsapp.soft_threshold_pct` you get one nudge per conversation suggesting
  `/compact` or `/new`, and past `whatsapp.hard_threshold_pct` the context is
  compacted for you and you are told afterwards. The check itself also arms the
  agent's own background compaction. A group reply the agent was not addressed
  in, and a conversation you switched off in the dashboard, get the compaction
  but no message: neither is a place the agent should speak up unasked.
- **Formatting**: Markdown is converted to WhatsApp's dialect (`*bold*`,
  `_italic_`, ` ``` `code` ``` `); headings become bold lines, bullets become
  `•`, links become `label (url)`.
- **Interactive choices** degrade to numbered text options: reply with the
  number. Tappable buttons are not used here. That is this channel's deliberate
  choice rather than a protocol limit, because whether a recipient's app renders
  a button sent from a personal linked device is not something we can promise.
- **Reconnect floods**: after a reconnect WhatsApp replays recent history;
  the channel drops replayed messages older than the connection moment
  instead of answering a backlog.
- **Read receipts / typing**: the agent shows "typing…" while working on a
  reply, and never marks your own self-chat as read on your behalf.

## Configuration reference

| Key | Default | Meaning |
|---|---|---|
| `whatsapp.enabled` | `false` | Main switch for the channel. |
| `whatsapp.dm_policy` | `"self"` | DM access policy (see above). |
| `whatsapp.allowed_wa_ids` | `[]` | Extra numbers for `allowlist` (digits, country code, no `+`). |
| `whatsapp.groups` | `[]` | Per-group participation rules (see above). |
| `whatsapp.db_path` | `""` | Read-only. The session store always lives at `<data home>/whatsapp/session.db`, because that path is what the sensitive-path protection matches. |
| `whatsapp.soft_threshold_pct` | `80` | Nudge you to `/compact` or `/new` once context passes this usage, checked at the end of each turn. |
| `whatsapp.hard_threshold_pct` | `95` | Compact automatically once context passes this usage, checked at the end of each turn. |
| `whatsapp.session_folder` | `""` | Optional sidebar folder for this channel's sessions. |

## Troubleshooting

- **"Channel enabled but the optional dependency is missing"** — install
  `kirocrew[whatsapp]` into the gateway's environment and restart.
- **Badge shows "The link was revoked"**: the phone revoked the link, or WhatsApp
  expired it. Nothing in the dashboard can restart pairing, so restart the gateway
  with the channel enabled and scan the fresh code. The old session file is
  replaced.
- **Badge shows "Paired, but not connected"**: a session exists and the channel is
  not carrying traffic. The line under the badge carries the gateway's own reason.
- **"Unlink" reported that the device is still linked**: WhatsApp refused the
  logout and the local session was kept on purpose so a retry can work. Retry, and
  if it keeps failing revoke the device from your phone under Linked devices.
- **Agent answers old messages after downtime** — it should not (see
  reconnect floods above); report with gateway logs if you see it.
- **Group replies missing** — check the group is in `whatsapp.groups`, its
  `mode` is not `off`, and (for unprompted replies) `rules` is non-empty and
  the cooldown has elapsed.
- **Not sure whether the channel can run at all** - run `kirocrew doctor`. Its
  **WhatsApp Integration** section reports whether the `whatsapp` extra is
  installed (a missing extra is a hard failure) and whether the linked-device
  session store exists at `~/.kiro/crew/whatsapp/session.db`. An unpaired store is
  a warning, not a failure: you pair from the dashboard with the gateway running,
  so a channel you just enabled is expected to report it.
- **A configured group is ignored** - check the gateway log at startup for a line
  naming configured groups that are not groups this account is in. A JID copied by
  hand is the usual cause; the group picker writes the exact form the gate matches.
- **An attachment in a group was not opened** - files are downloaded only for the
  account owner and numbers in `whatsapp.allowed_wa_ids`. Group membership admits
  someone to the conversation, not to your machine, so add the number to the
  allowlist if you want their photos and documents read.

## Attribution

Protocol layer by [neonize](https://github.com/krypton-byte/neonize)
(Apache-2.0) over [whatsmeow](https://github.com/tulir/whatsmeow). Echo
discipline, reconnect-flood handling, and group-gating patterns informed by
the OpenClaw project's WhatsApp bridge (MIT).
