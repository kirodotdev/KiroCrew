# Waiting-on-you cards (`waiting_on_you`)

When an agent is blocked on you, it can pin what it needs above the composer with the `waiting_on_you` tool. The card stays there across turns, loop cycles, reloads and gateway restarts.

An ask is one of two kinds:

- **An action**: something only you can do, such as "Post the Security Scope override on #38289". The card shows **Done** and an optional text field.
- **A decision**: one to six options. The card shows a button for each.

The card is labelled **Action for you**, in its own colour, so it is never mistaken for a live question card or a tool approval ("Waiting on you"). A live question card or approval sits above the stack and still wins on the sidebar row. A session can hold up to four asks; the newest shows in full and the rest fold under "N more".

## Tool input

```json
{
  "key": "scope-override",
  "title": "Post the Security Scope override on #38289",
  "detail": "Security Scope Review blocks merge until a writer posts the override.",
  "options": [
    {"label": "I posted it", "description": "I re-check the lane and continue"},
    {"label": "Hold the override", "description": "I keep the PR parked"}
  ]
}
```

| Field | Rule |
|---|---|
| `key` | Required. 1-40 characters of `a-z`, `0-9` and `-`, starting with a letter or digit. Calling again with a pinned key replaces that ask in place. |
| `title` | Required. One line, at most 120 characters. |
| `detail` | Optional markdown, at most 1,000 characters. |
| `options` | Optional, 0 to 6. Label at most 200 characters, description at most 500, no two labels alike. A label is sent as your message, so one starting with `/` or `@` or holding a `$name` skill token or a `[ Paste #` token is refused. No options makes an action card. |

Over-long text is refused, never cut, so the agent knows exactly what you see. A fifth new key is refused and the reply lists the pinned keys. A call identical to the pinned ask changes nothing. Text passes through credential and exfiltration-URL redaction before it is stored.

`waiting_on_you_clear` takes `key`, or `all: true`. The agent calls it when a blocker resolves without you.

Both tools work only in a dashboard chat session. Anywhere else the agent asks in plain text or ends its turn with `[OPTIONS: a | b]`. A conductor-mode worker is told to ask its conductor, as with `ask_question`. For a decision the agent needs before the current turn can continue, it uses [`ask_question`](agent-questions.md) instead.

## What ends an ask

| Event | Effect |
|---|---|
| You answer one card | That ask ends when your answer's message is written in the chat. Others stay. |
| You type a message, click an `[OPTIONS:]` chip, or edit and resend a message | Every ask ends: Kiro Crew cannot tell which ask a message answers. |
| You click ✕ | That ask ends. |
| The agent calls `waiting_on_you_clear`, or replaces the key | The ask ends, or is replaced. |
| 7 days pass since the ask last changed | It expires. |

A rewind or regenerate leaves the asks pinned; the agent still sees them and clears what no longer stands. A teammate's message, a channel message, and every automated message (an auto-nudge, a cron, a webhook, a sub-agent result, another session's `session_send`, an app) end nothing. A forked or transferred session starts with no asks, and deleting a session removes them.

## Answering

Your answer is sent as an ordinary message in the chat, the same send the composer makes, holding exactly what you chose or typed (or `Done` for an empty action answer). It never quotes the ask itself. It names the ask it answers; when the message is written Kiro Crew checks that the ask is still pinned. An answer to an ask that is already gone, or that already has an answer waiting in the queue from another window, is refused before it is sent. If the agent replaces or clears the ask while your answer waits in the queue, the message still reaches the agent but ends no ask, so the asks still pinned stay. If the agent is busy, the answer is queued as its own message, never merged with another and never steered into the running turn; the card stays until the queued message is written, so cancelling it or stopping the agent leaves the card in place. A refused send (for example while the composer is locked) keeps the card and any note you typed, and shows the usual send error.

## What the agent sees

Each turn while an ask is pinned, or one ended since the agent's last turn, carries a `[WAITING ON YOU]` block. It lists each pinned ask by key, title and age, and names each ask that was answered, dismissed, closed by your message or expired, once, with its title, detail and option labels, so the agent can declare it again if it still stands. On the turn your answer starts, the block says which ask your message answers. The block is reference data written by Kiro Crew, never a message from you, and stays under 6,000 characters.

## Storage

Asks live in `waiting_on_you.json` under the data home. Each record carries a keyed MAC; an edited or foreign record is dropped when the gateway loads the file, and an unreadable or oversized file loads as empty. Incognito and temporary sessions keep their asks in memory only, so a restart drops them. Pinning an ask rings no bell and holds, stops or wakes nothing.
