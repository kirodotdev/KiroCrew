# Channel capabilities

One table for the question every channel doc otherwise answers separately: does
this channel stream, does it render buttons, can it take a file, how long a reply
fits, and how long an approval prompt waits. Read it before you pick a channel,
or when a channel behaves differently from the one you are used to.

A ✅ means Kiro Crew supports that behaviour on that channel today, not that the
platform could support it.

## The matrix

| | Slack | Discord | Telegram | Teams | Webex | WeCom | Weixin | iMessage | WhatsApp | Feishu |
|---|---|---|---|---|---|---|---|---|---|---|
| Streams the reply as it is written | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ | ✅ | ❌ |
| Edits a message it already sent | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ |
| Adds emoji reactions | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ |
| Accepts a file you send | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ |
| Sends a file back to you | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ |
| Native widget (buttons, cards) | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Threads a conversation | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Renders markdown tables natively | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ |
| Reply length before splitting | 3900 chars | 1900 chars | 4000 chars | 16000 chars | 1750 chars (7000 bytes) | 5120 chars (20480 bytes) | 4000 chars | 4000 chars | 4096 chars | 4000 chars |
| Tappable choices per prompt | 10 | 25 | 25 | 5 | 5 | 0 | 0 | 0 | 0 | 0 |
| Approval prompt waits | 120s | 300s | 300s | 300s | 300s | — | — | — | 300s | — |
| Agent can message you first | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| Dashboard link is two-way | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

## Reading the rows

**Reply length** is where a long answer is split into more than one message.
Webex and WeCom cap in UTF-8 bytes rather than characters, so their character
figure is the byte budget divided by four — the worst case for non-ASCII text.
An ASCII-only reply on those channels fits far more than the character figure
suggests.

**Tappable choices** is the total number of interactive options one prompt may
present. Above the cap, the remainder degrades to a numbered list in the message
body rather than being dropped. A channel showing `0` renders no widget at all:
every choice arrives as a numbered line, and you answer by typing the number.

**Native widget** gates whether a channel builds a platform card for a tool
approval or a choice list. Discord streams and threads well but declares no rich
widget, so its approvals are text.

**Dashboard link is two-way** means connecting the channel from the dashboard
marks the binding as an inbound target, so your reply in the channel continues
the same session. Where it is ❌ the link is outbound only — the dashboard can
push to the channel, but a reply there starts its own session. Slack is a special
case: it routes inbound traffic through its own thread index rather than this
marker, so a Slack thread does continue its session.

## Approval timeouts

Only six channels ask you at all. Slack, Discord, Telegram, Teams, Webex and
WhatsApp install an approval decider, so a tool that needs permission produces a
prompt and waits:

- **Slack: 120 seconds.** Slack has its own approval path with a shorter window
  than every other channel.
- **Discord, Telegram, Teams, Webex, WhatsApp: 300 seconds.**

An unanswered prompt is **denied**, never approved — the timeout never means yes.

**WeCom, Weixin, iMessage and Feishu never prompt.** None of them can render
approve/deny controls, so in `interactive` mode a tool needing permission is
refused straight away: nothing is posted and there is no window to answer in. The
`—` in that row means exactly this, not "unlimited". To run tools on those
channels, set the approval mode to `auto` or `trust` — see the channel's own
guide.

`agent.tool_approval_timeout_secs` (default 600) does **not** govern any of
these. It applies only to the dashboard chat path. Changing it will not lengthen
or shorten the window on any messaging channel.

## Owner DM targets

`send_message` has two different routes, and they differ in what they can reach.

The **owner-DM route** (`session=<channel>`) infers a recipient: it needs exactly
one allow-listed destination configured on that channel, and it needs the channel
to be able to send unprompted. Three channels cannot use it:

- **WeCom** and **Weixin** are excluded outright. Both fold identities learned
  from inbound traffic into their send roster, so "the owner" could resolve to any
  peer who once messaged the bot, and guessing is worse than refusing.
- **Feishu** is accepted but cannot deliver, because it only ever replies to an
  inbound message and has nowhere to put an unprompted one.

In all three cases the call falls back to a dashboard notification and says so
rather than reporting success. An ambiguous allow-list or a channel that is not
connected falls back the same way.

The **conversation route** (`channel_type`) addresses the conversation you are
already in rather than inferring a recipient, so it *does* work on WeCom and
Weixin. Add a destination id to aim it somewhere specific; the channel's
allow-list is re-checked when the message is sent.

## Related docs

Per-channel setup, access control, and behaviour live in each channel's own
guide: [Slack](slack-integration.md), [Discord](discord-integration.md),
[Telegram](telegram-integration.md), [Teams](teams-integration.md),
[Webex](webex-integration.md), [WeCom](wecom-integration.md),
[Weixin](weixin-integration.md), [iMessage](imessage-integration.md),
[WhatsApp](whatsapp-integration.md), [Feishu](feishu-integration.md).

The contracts these values come from are described in
[`docs/system-specs/modules/messaging.md`](https://github.com/kirodotdev/KiroCrew/blob/main/docs/system-specs/modules/messaging.md),
the spec that owns
the channel-neutral transport contracts.
