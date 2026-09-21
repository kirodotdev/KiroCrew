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
| Native widget (card, inline keyboard) | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Threads a conversation | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Renders markdown tables natively | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ |
| Reply length before splitting | 3900 chars | 1900 chars | 4000 chars | 16000 chars | 1750 chars (7000 bytes) | 5120 chars (20480 bytes) | 4000 chars | 4000 chars | 4096 chars | 4000 chars |
| Tappable choices per prompt | 10 | 25 | 25 | 5 | 5 | 0 | 0 | 0 | 0 | 0 |
| Approval prompt waits | 120s | 300s | 300s | 300s | 300s | — | — | — | 300s | — |
| Agent can message you first | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| Dashboard link is two-way | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Answers a send with a message id | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ |
| Parses `@everyone`-style mentions | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |

## Reading the rows

**Reply length** is where a long answer is split into more than one message.
Webex and WeCom cap in UTF-8 bytes rather than characters, so their character
figure is the byte budget divided by four — the worst case for non-ASCII text.
An ASCII-only reply on those channels fits far more than the character figure
suggests.

**Tappable choices** is the total number of interactive options one prompt may
present. Above the cap, the remainder degrades to a numbered list in the message
body rather than being dropped. A channel showing `0` renders no widget at all —
see [Choices on a channel with no buttons](#choices-on-a-channel-with-no-buttons).

**Native widget** gates whether a channel builds its own platform control for a
tool approval or a choice list — a Block Kit block on Slack, an Adaptive Card on
Teams and Webex, an inline keyboard on Telegram. It is not the same question
as buttons: Discord shows ❌ here and still renders Approve/Deny buttons and
tappable `[OPTIONS:]` rows, which is what its `25` in the row below means.

**Dashboard link is two-way** means connecting the channel from the dashboard
marks the binding as an inbound target, so your reply in the channel continues
the same session. Where it is ❌ the link is outbound only — the dashboard can
push to the channel, but a reply there starts its own session. Slack is a special
case: it routes inbound traffic through its own thread index rather than this
marker, so a Slack thread does continue its session.

**Answers a send with a message id** (`returns_message_id`) is plumbing rather
than a feature you use: it says which convention the channel follows when a send
does not go out. Most platforms hand back an id, so an empty id there means
refused or dropped, and a multi-part reply stops instead of posting the rest
after a hole. WeCom's unprompted send and Feishu's reply carry no id at all, so
on those two an empty id is the SUCCESS value and a real failure raises instead.
Kiro Crew reads the declaration wherever it has to judge delivery — a
dashboard-addressed send, an owner DM, a quoted inbound copy — so a ❌ here is a
different convention, not a missing capability.

One caveat sits inside the ✅ column. **iMessage declares the strict reading, and
its bridge does not keep it:** the bridge reports the message id as best-effort, so
a delivered message can come back with no id and be recorded as undelivered. The
cell reports what the channel declares, which is what the rest of Kiro Crew acts
on; the bridge is the exception to it, and the two should agree.

**Parses `@everyone`-style mentions** (`mention_grammars`) decides whether text
the agent did not write gets a defang first: a zero-width space after every `@`
and every `<!`, so a quoted or model-authored `@everyone` cannot mass-notify a
room. Every channel gets it except Webex, which parses no broadcast grammar at
all and whose allow-list *is* email addresses — defanging there would make every
address the agent prints uncopyable.

## Choices on a channel with no buttons

Five channels show `0` tappable choices: WeCom, Weixin, iMessage, WhatsApp and
Feishu. On four of them the choices still arrive in full. WeCom, Weixin, iMessage
and Feishu turn the whole `[OPTIONS:]` list into numbered lines in the message
body, and you answer by typing the number as an ordinary message — nothing to
configure, nothing to tap, nothing dropped with the trailer. It is the same helper
that handles overflow on a channel that does have buttons
(`render_options_as_text` is `apply_options_cap` with zero widget slots), so the
numbering reads the same everywhere.

**WhatsApp drops the list instead**, and that is a gap rather than a setting. Its
renderer removes a completed `[OPTIONS:]` trailer from the reply rather than
numbering it, so a question whose choices live only in that trailer arrives with
the choices missing. Nothing you can configure changes it today.

Tool approval is a separate question from choices, and it takes a different path.
WeCom, Weixin, iMessage and Feishu install no approval decider at all, so in
`interactive` mode a tool needing permission is refused rather than asked about.
WhatsApp does install one, so an approval — unlike a choice list — does reach you,
as a numbered prompt with the usual 300-second window.

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

## iMessage: when a reply goes out as SMS

`imessage.service` picks the service outbound replies use: `imessage` (the
default), `sms`, or `auto`. It is one channel-wide setting, not a per-conversation
one — every outbound reply uses it, whichever service the incoming message
arrived over. Any other value falls back to `imessage` when the config loads.

On the default, Kiro Crew names no service on the send at all, so the bridge uses
iMessage and the SMS path is never exercised. `sms` and `auto` are passed through
to the `imsg` bridge verbatim, and the bridge decides from there: `auto` is its
own per-send fallback to SMS when it cannot deliver over iMessage. Kiro Crew does
not probe the handle, so it cannot tell you in advance which service a given
number will get, and the reply carries no marker saying which one was used.

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
