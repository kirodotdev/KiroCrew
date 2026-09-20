# Jev decisions

Jev can do two things for a sampled conversation: choose its automatic skill, and flag a risky tool call on the card that reports it. Only the first one decides anything -- see "Flagging risky tool calls" below. Skill selection: Jev can choose an automatic skill for a sampled conversation. When enabled, it receives a short message excerpt and a menu of eligible skill names and descriptions. It can also receive some of the conversation so far, but only if you ask for that. Its valid answer changes the selected skill; a timeout or failed request keeps the normal trigger-matching result. The feature is off by default.

## What changes

Only automatic skill selection uses Jev. Skill deduplication and scheduled notifications do not change. Mandatory skills, custom-agent exclusions, project access rules and the automatic skill limit still apply.

| State | Skill selection |
|---|---|
| Disabled | Normal trigger matching |
| Enabled, outside the sample | Normal trigger matching |
| Enabled, sampled, valid answer | Jev's selection |
| Timeout, refusal or invalid answer | Normal trigger matching |

A valid answer can choose one skill or explicitly choose none. Choosing none is not a failed request. There is no shadow mode that asks Jev only to discard its answer.

## Configure before enabling

Use Settings > Developer > Feature Previews for the Decisions switch. It records your consent in `decisions_consent.json` in the gateway's data directory, so it applies across devices. That file is deliberately separate from `config.json`: an agent can edit `config.json`, and an agent must not be able to switch on the sending of your own messages. Only the dashboard owner can flip the switch. The card shows the address messages would be sent to, and your consent is recorded for that address: if `provider.endpoint` is changed later, nothing is sent until you turn the switch off and on again. An older backend without this switch keeps it disabled.

The remaining settings live in `config.json`:

The configuration shape is:

```json
{
  "decisions": {
    "bucket": 10,
    "history_budget_chars": 0,
    "provider": {
      "endpoint": "https://api.typesafe.ai/v1/systemone",
      "api_key": "secret://TYPESAFE_API_KEY",
      "model": "jev-latest",
      "timeout_ms": 1000
    }
  }
}
```

Create the API-key secret through the existing [secrets vault](secrets-vault.md) under the name `TYPESAFE_API_KEY`; that is the only vault entry this feature reads, and only for the default Jev endpoint. The reference above is a placeholder, not a working key.

`bucket` chooses a percentage of sessions. It is a fixed sample, not a random draw per message. A session stays selected or unselected while its key and bucket remain unchanged. `0` samples none and `100` samples all otherwise eligible sessions. A value that is not a whole number reads as `0`, so a typo never widens the sample.

`history_budget_chars` bounds how much of the conversation so far is sent with one decision, in characters, on top of the current message. **Its default is `0`: no earlier turns are sent.** Raise it and earlier user and assistant turns are added newest first until the budget is spent, with the last one admitted clipped to fit. At most the 20 most recent turns are read, so a budget far above a few thousand characters stops adding turns. Tool output is never sent. A value that does not parse reads as `0`, so a typo never widens what leaves the machine.

This setting alone does not permit the transfer. Your consent record holds a **ceiling** for it, and Kiro Crew sends the smaller of the two. Lowering the setting works on its own; raising it above the ceiling does nothing until you consent again with the larger figure. The reason is that `config.json` can be written by an agent working on your machine, while the consent record cannot: if the permission lived only in the settings file, an agent reading your conversation could raise it and send that conversation. A consent recorded before this ceiling existed has no figure in it, which reads as `0` — so an upgrade never starts sending your earlier turns.

`skills.max_triggered` must be greater than zero to allow automatic selection. Its default is zero, which disables automatic selection even when the Decisions switch is on. Jev selects at most one skill and does not raise that limit.

After setting the provider and sampling values, enable the switch only if the data transfer below is acceptable. Turn it off to return to normal trigger matching. Old `preview` and per-point mode values do not enable this new behavior.

## Data and waiting time

Enabling Jev allows the message excerpt and candidate skill descriptions to leave the machine. It does not send your earlier turns unless you both raise `history_budget_chars` and consent to a ceiling for it, after which that many characters of earlier user and assistant turns from the same conversation leave the machine as well. Credential and suspicious-URL checks refuse matching requests, but they are not a guarantee that all private content is detected. Do not enable the feature for content that must stay local.

A sampled selection waits for a bounded answer. `timeout_ms` controls the provider budget; its default is 1000 milliseconds, and the wait is capped at ten seconds whatever that value says. A missing key, unavailable provider or short budget can make the feature fall back without changing the selected skills. There is no automatic retry.

## What a decision leaves on its reply

When a sampled turn asks Jev which skill to load, the reply that turn produces carries a record of that decision. The record belongs to one reply. It is never copied onto a later one, and a turn that made no decision carries nothing at all.

The record travels with the message, not in a side channel, so it is there when you scroll back to that reply and there when a second window opens the same chat. It holds what trigger matching chose, what Jev chose, whether the two agreed, the probability Jev reported and a few counts about the menu it was given. It does not hold your message or the skill descriptions.

The chat draws that record as a one-line strip under the reply it belongs to. A reply with no record looks exactly as it always did, which is every reply while the switch is off. The record is also readable through the chat history the dashboard already serves, and through the log below.

You can record whether a choice was right. The verdict is `right` or `wrong`, and it names which of the two answers you are judging -- Jev's or the normal trigger-matching one. Sending it again with a different verdict records the change of mind; sending it with the verdict spelled out as `null` takes your earlier one back. Leaving the field out altogether is refused instead, so a request that lost it does not read as taking a verdict back. Each of these appends one row to the day-file described below and never edits a row already there, so the log reads as a history rather than a current opinion. If the day-file is full the verdict is refused rather than quietly dropped, so a recorded verdict means a written one.

The thumbs on the strip are that button, and the thumbs on the risk badge below are the same one. Either way it is an owner-only request (`POST /api/decisions/feedback`), refused for anyone but the dashboard owner, for the same reason the Decisions switch is. The verdicts land in the same daily JSONL files as the decisions, so counting them is a `jq` job over `~/.kiro/crew/decisions/*.jsonl`.

## Flagging risky tool calls

In a session that approves its own tool calls, nothing stops to describe what is about to run. Jev can put a small note on those cards: **Jev: risky (0.88)** under the tool line, with a thumbs pair beside it.

It is a note and only a note. Kiro Crew decides whether a tool call may run exactly as it did before, using your permission setting alone, and Jev is asked what it thinks alongside that. Nothing here changes who may run what, and nothing you can set here does either. The note is not a promise that the call went ahead: a security rule or one of your own hooks can still stop a call that carries one, and the audit log is where what happened is recorded. The one thing the note costs is a short wait -- Jev is asked before the next step of the turn is read, so a flagged call can be approved a moment later than it would have been. The wait is capped, and only sessions the switch covers pay it.

You have to turn this on separately. The Decisions switch covers your message text and your skill descriptions; flagging tool calls also sends the name and arguments of each call, which is more than you agreed to when you turned that switch on. So there is a second switch under it -- **Also send tool-call arguments so Jev can flag risky calls** -- and it starts off, including for anyone who already had the main switch on before this existed. Turning the main switch off and on again keeps your answer to the second one; turning it off is what clears it.

| Your session | What you see |
|---|---|
| The second switch is off | Nothing -- no tool arguments are sent and no notes appear |
| Asks you before each tool call | Nothing new -- you are already reading the call |
| Trusts the session, or YOLO, and Jev says `safe` | Nothing -- the card looks as it always did |
| Trusts the session, or YOLO, and Jev says `caution` or `risky` | The note, with a score and thumbs |
| Timeout, refusal or invalid answer | Nothing |

A session that asks you is never annotated, because you are the one looking at the call. The note exists for the sessions where nobody is.

Jev is asked about one call at a time, and at most twenty times in one turn. A turn that runs more tools than that keeps running normally; the calls past the twentieth simply carry no note, and the log says where the count stopped.

What leaves the machine for one of these questions is the tool's name, its arguments and a short excerpt of the message that led to the call. Credentials and suspicious URLs in those arguments are replaced with a placeholder BEFORE the question is sent -- a key in an `aws` command is ordinary, and refusing to look at it would mean the note never appears on the calls most worth a second look. The same waiting time and the same cap apply as for skill selection, so a slow answer costs the note, not the call.

The thumbs say whether Jev read the risk right. They are the same owner-only verdict described above, filed against that one call.

Each answered call writes two rows in the log below: one for the question and one for the answer. A `safe` answer is recorded too, even though it draws nothing, so you can tell how often the note would have been wrong to appear. The rows name the tool, the tier, the score and which grant approved the call -- `trust`, `trust_scope` or `yolo`.

## Basic logs

Operational records are JSONL day-files under the gateway's data home, in the `decisions` directory. That directory is read-only to agents working on your machine -- by name, so a link planted at that name does not stand in for it -- so a verdict in it is one you gave. They contain the point name, hashed session identifier, elapsed time, bounded answer data and error categories. They do not contain the message body, conversation history, candidate descriptions or credentials.

A skill selection writes one row for the question asked, carrying a `turn_id` and the number of candidates, and — when a usable answer came back — one further row for the outcome, carrying both selections: `baseline` is what trigger matching would have injected, `jev` is what was injected, `agree` says whether the two sets match, `p` is the answer's probability, and `tokens_saved` estimates the skill-body characters the difference saves, divided by four. That estimate is a rough one, and a negative value means the selection cost more than trigger matching would have. `history_chars` and `truncated` say how much conversation the request carried. A refused, timed-out or unusable turn writes only the question row, with its error category, because an agreement figure needs an answer to compare against. So one selection is two rows, and a row count is not a count of decisions.

These are diagnostic records, not a billing report. This feature does not provide a decisions report command. Each day-file stops growing at 8 MiB (further rows that day are dropped, with one warning), and day-files older than 14 days are deleted by the next write, so the log stays a bounded number of bounded files. A missing row alone is not proof that an answer was applied.

The provider mapping is tested locally against a loopback server. A real Jev call requires your API key; local tests do not establish real service latency or account compatibility.
