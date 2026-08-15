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

3. Open **Settings → Channels → WhatsApp** and click **Pair device**. Scan
   the QR with your phone: WhatsApp → Settings → Linked devices → Link a
   device. The QR rotates every ~20 seconds; the panel follows it until the
   scan lands.
4. The badge flips to **Connected**. Pairing state persists in
   `~/.kiro/crew/whatsapp/session.db` — you scan once, not per restart.
   **Unlink** from the panel (or from your phone's Linked devices) to revoke.

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
whatever the policy.

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

Group JIDs appear in the Settings panel's group picker once the channel is
connected (or ask the agent in your self-chat: "list my WhatsApp groups").

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
| `/compact` | Compact the current session's context. |

Operator only: commands from anyone else are treated as plain text.

## Behaviour notes

- **No streaming, no edits**: WhatsApp cannot edit a message into shape, so
  the agent sends its answer once, complete. Long answers arrive as multiple
  messages split at paragraph/code boundaries (4096-char cap each).
- **Formatting**: Markdown is converted to WhatsApp's dialect (`*bold*`,
  `_italic_`, ` ``` `code` ``` `); headings become bold lines, bullets become
  `•`, links become `label (url)`.
- **Interactive choices** degrade to numbered text options — reply with the
  number. (Buttons are a Business-API feature; linked devices don't get them.)
- **Reconnect floods**: after a reconnect WhatsApp replays recent history;
  the channel drops replayed messages older than the connection moment
  instead of answering a backlog.
- **Read receipts / typing**: the agent shows "typing…" while working on a
  reply, and never marks your own self-chat as read on your behalf.

## Configuration reference

| Key | Default | Meaning |
|---|---|---|
| `whatsapp.enabled` | `false` | Master switch. |
| `whatsapp.dm_policy` | `"self"` | DM access policy (see above). |
| `whatsapp.allowed_wa_ids` | `[]` | Extra numbers for `allowlist` (digits, country code, no `+`). |
| `whatsapp.groups` | `[]` | Per-group participation rules (see above). |
| `whatsapp.db_path` | `""` | Override the session DB location. |
| `whatsapp.soft_threshold_pct` | `80` | Suggest `/compact` past this context usage. |
| `whatsapp.hard_threshold_pct` | `95` | Force compaction past this usage. |
| `whatsapp.session_folder` | `""` | Optional sidebar folder for this channel's sessions. |

## Troubleshooting

- **"Channel enabled but the optional dependency is missing"** — install
  `kirocrew[whatsapp]` into the gateway's environment and restart.
- **Badge shows "logged out"** — the phone revoked the link (or WhatsApp
  expired it). Re-pair from the Settings panel; the old session DB is
  replaced.
- **Agent answers old messages after downtime** — it should not (see
  reconnect floods above); report with gateway logs if you see it.
- **Group replies missing** — check the group is in `whatsapp.groups`, its
  `mode` is not `off`, and (for unprompted replies) `rules` is non-empty and
  the cooldown has elapsed.

## Attribution

Protocol layer by [neonize](https://github.com/krypton-byte/neonize)
(Apache-2.0) over [whatsmeow](https://github.com/tulir/whatsmeow). Echo
discipline, reconnect-flood handling, and group-gating patterns informed by
the OpenClaw project's WhatsApp bridge (MIT).
