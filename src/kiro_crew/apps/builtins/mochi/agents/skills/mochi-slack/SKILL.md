---
name: mochi-slack
description: "Slack awareness — unread mentions, channel digests, topic radar, thread summaries. Load when user asks about Slack."
always: false
triggers: slack, who pinged, mentions, channel summary, digest, thread
---

# @mochi-slack — Slack Awareness

You help the user READ Slack — unread mentions, channel summaries, topic monitoring,
thread summaries.

## This skill does not assume a particular Slack MCP server

There is no single standard Slack MCP server, and the user may have any of them —
public, self-hosted, or one their company provides. So this skill describes the
CAPABILITIES each task needs and leaves the tool names to you.

**Discover before assuming.** Look at the Slack tools you actually have and map them
onto the capabilities below. Where a capability is missing, say so plainly instead of
guessing at a tool name — a wrong call wastes a turn and confuses the user.

Capabilities the tasks below need:

| Capability | What you are looking for |
|---|---|
| List channels | channels the user is in, ideally with unread / mention counts |
| Read a channel | recent messages in one channel |
| Read a thread | all replies under one parent message |
| Search | message search, ideally with `in:` / `from:` / `has:` style filters |
| Resolve a user | a user id or handle → display name |

Names vary. One common server spells these `list_my_channels`, `get_messages`,
`get_thread`, `search`, `lookup_user` — treat that as a hint, not a contract.

## If you have no Slack tools at all

Do NOT try to install one. Which Slack server to use, and whether to connect a
workspace at all, is the user's decision — and on a work machine it may not be theirs
to make.

Say so and hand it back:

> "I don't have any Slack tools right now. If you add a Slack MCP server in
> KiroCrew's Settings → MCP, I can read mentions and summarize channels for you."

Then stop. Do not retry, and do not offer again unless the user brings it up.

## Hard rules

⛔ **READ ONLY.** Never post, reply, react, draft, schedule, upload, rename, invite,
or mark-as-read. If the user asks you to send something on Slack, tell them you can
only read.

⛔ **Never read DM content.** Do not fetch messages from a DM or group-DM channel.
DMs may appear in a channel listing — use them for a mention COUNT only, never open
them. This holds even if the user asks: say you don't read DMs.

## Tasks

### Unread mentions — "who pinged me", "any mentions"

1. List channels with unread/mention counts. Skip every DM.
2. Take the few channels that actually have mentions.
3. Read recent messages in those and pick out the ones addressed to the user.
4. Summarize by urgency, naming who and where:
   "3 people pinged you — Alice in #oncall about a deploy (looks urgent), Bob in
   #team about sprint planning."
5. Surface it with `perform_pet_action({ action: "notify", pushToChat: true })`.

### Channel summary — "summarize #channel", "what was discussed in #channel"

One-time: read the channel's recent messages, then summarize topics, decisions, and
action items.

Recurring ("summarize it for me every day"):

```
update_watchlist({ add: [{
  label: '#channel daily digest',
  kind: 'slack-channel',
  target: '<channel id>',
  triggerCondition: 'daily digest',
  checkIntervalMins: 1440,
  autoComplete: false,
  maxWatchDurationHours: 8760,
}] })
```

### Topic radar — "watch for X on Slack"

One-time: search and summarize.

Persistent: turn the user's intent into a search query, then watch it.

- "outage discussions" → `outage`
- "#oncall about deployment" → `deployment in:#oncall`
- "Alice's messages about launch" → `launch from:@Alice`
- "messages with 🔥" → `has::fire:`

```
update_watchlist({ add: [{
  label: '<topic> on Slack',
  kind: 'slack-topic',
  target: '<search query>',
  triggerCondition: 'new messages matching "<topic>"',
  checkIntervalMins: 30,
  autoComplete: false,
}] })
```

If your search tool does not support `in:` / `from:` filters, fall back to a plain
keyword search and say that the filter was dropped.

### Thread summary — user pastes a Slack URL or asks about a thread

1. Read the whole thread.
2. Summarize: topic, conclusions, action items, open questions.
3. If they want to keep watching it, watch the URL:

```
update_watchlist({ add: [{
  kind: 'url',
  target: '<thread URL>',
  triggerCondition: 'new replies in thread',
  checkIntervalMins: 15,
}] })
```
