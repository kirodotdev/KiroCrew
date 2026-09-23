# Jev decisions

Jev can answer four questions about a sampled conversation, and each one has to be switched on separately. It can also put a note on a risky tool call, which decides nothing at all -- see [Flagging risky tool calls](#flagging-risky-tool-calls) below.

**Which skill to load.** Jev receives a short message excerpt and a menu of eligible skill names and descriptions. Its valid answer changes the selected skill; a timeout or failed request keeps the normal trigger-matching result.

**Which model answers a chat message.** Jev judges how hard the message is -- simple, medium or complex -- and the chat runs on the model you mapped that level to. This happens only while the session's model is set to **Auto (Jev)** in the chat model picker. A timeout, a failed request, or a model your account cannot run leaves the chat on the model it was already using. See [Letting Jev pick the model](#letting-jev-pick-the-model) below.

**What a mid-turn message does.** Jev judges whether a message you send while the assistant is still working should steer that turn or wait for the next one. This happens only for a message you send with **Auto (Jev)** on the send button. A timeout or a failed request steers, which is what the send button has always done. See [Letting Jev choose steer or queue](#letting-jev-choose-steer-or-queue) below.

**Which recalled memories reach the prompt.** When the assistant asks its own memory a question, Jev says which of the closest matches are worth putting in the prompt. This happens only while the recalled-memory switch under the Decisions switch is on. A timeout or a failed request keeps every match the search found. See [Choosing which recalled memories come back](#choosing-which-recalled-memories-come-back) below.

All four are off by default. Turning on the Decisions switch does not start any of them: skill selection also needs `skills.max_triggered` above zero, model routing also needs you to pick **Auto (Jev)** in the chat model picker, mid-turn handling also needs you to pick **Auto (Jev)** on the send button, and memory narrowing also needs its own switch under the Decisions switch.

## What changes

Three things use Jev: automatic skill selection, which model answers a chat message, and -- only if you pick it on the send button -- what happens to a message you send while the assistant is still working (see "Letting Jev choose steer or queue" below). Skill deduplication and scheduled notifications do not change. Mandatory skills, custom-agent exclusions, project access rules and the automatic skill limit still apply. Scheduled jobs, sub-agents, sessions running on a connected crew, and anything an app sends are never routed -- each already has its own model setting, and nobody is watching what those cost at the moment they run.

| State | Skill selection |
|---|---|
| Disabled | Normal trigger matching |
| Enabled, outside the sample | Normal trigger matching |
| Enabled, sampled, valid answer | Jev's selection |
| Timeout, refusal or invalid answer | Normal trigger matching |

A valid answer can choose one skill or explicitly choose none. Choosing none is not a failed request. There is no shadow mode that asks Jev only to discard its answer.

## Configure before enabling

Use Settings > Developer > Feature Previews for the Decisions switch. Three of the things Jev can do need a switch of their own, underneath it, and each starts off even for someone who already had the main one on: **Also send tool-call arguments so Jev can flag risky calls**, **Also send the conversation and tool-call inputs so Jev can score compaction**, and **Also send snippets of recalled memories so Jev can drop the ones that do not help**. Turning the main switch on alone gives you automatic skill choice and the mid-turn send mode; the other three do nothing until you turn their own switch on, because each sends a category of your content the main switch never described. It records your consent in `decisions_consent.json` in the gateway's data directory, so it applies across devices. That file is deliberately separate from `config.json`: an agent can edit `config.json`, and an agent must not be able to switch on the sending of your own messages. Only the dashboard owner can flip the switch. The card shows the address messages would be sent to, and your consent is recorded for that address: if `provider.endpoint` is changed later, nothing is sent until you turn the switch off and on again. An older backend without this switch keeps it disabled.

The remaining settings live in `config.json`:

The configuration shape is:

```json
{
  "decisions": {
    "bucket": 10,
    "history_budget_chars": 2000,
    "model_route": {
      "simple": "claude-haiku-4.5",
      "medium": "claude-opus-4.8",
      "complex": "claude-fable-5.1"
    },

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

`bucket` chooses a percentage of sessions. It is a fixed sample, not a random draw per message. A session stays selected or unselected while its key and bucket remain unchanged. `0` samples none and `100` samples all otherwise eligible sessions. Its default is `100`, so with the switch on and no `bucket` set, every otherwise-eligible session is sampled. A value that is not a whole number reads as `0`, so a typo never widens the sample.

`history_budget_chars` bounds how much of the conversation so far is sent with one decision, in characters, on top of the current message. **Its default is `2000`, about the last two or three turns.** It is only what the decision ASKS for: your consent records a ceiling of its own, the smaller of the two is what goes, and that ceiling is `0` until you set one, so a fresh install still sends your new message alone. Inside the ceiling, earlier user and assistant turns are added newest first until the budget is spent, with the last one admitted clipped to fit. At most the 20 most recent turns are read, so a budget far above a few thousand characters stops adding turns. Tool output is never sent, by any of the features on this page. A value that does not parse reads as the default, and your consented ceiling clamps that too, so a typo never sends more than you reviewed.

This ceiling does not govern compaction scoring, which is a separate switch and sends a whole transcript when you turn it on — see [Measuring what a compaction should keep](#measuring-what-a-compaction-should-keep) below.

This setting alone does not permit the transfer. Your consent record holds a **ceiling** for it, and Kiro Crew sends the smaller of the two. Lowering the setting works on its own; raising it above the ceiling does nothing until you consent again with the larger figure. The reason is that `config.json` can be written by an agent working on your machine, while the consent record cannot: if the permission lived only in the settings file, an agent reading your conversation could raise it and send that conversation. A consent recorded before this ceiling existed has no figure in it, which reads as `0` — so an upgrade never starts sending your earlier turns.

`model_route` says which model answers a message at each difficulty level. **Every level starts empty, which means "leave it alone".** No model is named for you on purpose: accounts differ in which models they are offered, and a name you cannot use would fail on the first message rather than when you set it. The block above is the example to copy from — put in the ids your own model picker shows.

An empty level does not turn the feature off. Jev is still asked, the answer is still recorded, and the reply still shows it — it reads `complex → (unpinned)`. The message runs on your own model: the one the chat was on before routing ever moved it, so an earlier message routed to a cheap level does not keep this one there. That is on purpose: you can watch which level your messages land in for a while, and then pin only the levels worth moving. Kiro Crew can only move a chat back to a model it can name, so a chat whose model your backend never reported stays where it is.

The three keys above are the only ones read; anything else is ignored. `auto` means the same as empty. If you name a model your account cannot run, that message also stays put, and the log below says which of the two happened, so nothing is dropped silently.

This setting alone changes nothing: a chat is only routed while its model is set to **Auto (Jev)**.

`skills.max_triggered` must be greater than zero to allow automatic selection. Its default is zero, which disables automatic selection even when the Decisions switch is on. Jev selects at most one skill and does not raise that limit.

After setting the provider and sampling values, enable the switch only if the data transfer below is acceptable. Turn it off to return to normal trigger matching. Old `preview` and per-point mode values do not enable this new behavior.

## Letting Jev pick the model

Open the model picker under the chat box and choose **Auto (Jev)**. The entry appears only when the Decisions switch is on and your organisation allows the feature, so if you do not see it, turn the switch on first.

That alone gets you the reading: each message is judged and the reply says which level it landed in, while every level is still unpinned so nothing moves. Fill in `model_route` above when you want messages actually routed — each level you pin starts taking effect on the next message.

From then on, each message you type is judged once, and a message whose level you have pinned is answered by that model. A message that needs a plan or a trade-off can go to a stronger model; a rename or a lookup can go to a cheaper one. A level you have not pinned is reported and left alone.

**This can cost more.** Routing a message to a stronger model spends more than staying on your usual one. You chose that when you picked **Auto (Jev)**, and you can undo it in one click: pick any model in the same picker and the routing stops immediately. Picking a model by hand is never overridden -- it is your answer to the same question Jev was being asked.

A few things worth knowing. The choice belongs to one chat, not to the whole app, and it lasts until you change it or the gateway restarts — a restart leaves the chat on its usual model, and you pick **Auto (Jev)** again. It is not written to disk on purpose: the file it would live in can be edited by an agent working on your machine, and this choice can cost you money, so nothing but your own click in the picker turns it on. A message sent by a scheduled job, a sub-agent or an app is never routed. The chat does not switch back after each message: if Jev is unavailable for the next one, that message runs on whatever the last one used. And the reply carries a small line saying which level Jev picked, which model answered, and which model would have answered otherwise -- with a thumbs pair, so you can say it got it wrong.

## Data and waiting time

Enabling Jev allows the message excerpt to leave the machine -- with the candidate skill descriptions for a skill choice, and on its own for a model choice. It does not send your earlier turns unless you consent to a ceiling for them, after which that many characters of earlier user and assistant turns from the same conversation leave the machine as well. Credential and suspicious-URL checks refuse matching requests, but they are not a guarantee that all private content is detected. Do not enable the feature for content that must stay local.

A sampled selection waits for a bounded answer. `timeout_ms` controls the provider budget; its default is 1000 milliseconds, a value at or below zero is floored to 1 millisecond rather than disabling the timeout, and the wait is capped at ten seconds whatever that value says. A missing key, unavailable provider or short budget can make the feature fall back without changing the selected skills. There is no automatic retry.

## What a decision leaves on its reply

When a sampled turn asks Jev something, the reply that turn produces carries a record of each decision. A record belongs to one reply. It is never copied onto a later one, and a turn that made no decision carries nothing at all. A turn that made both decisions carries both, one line each.

The records travel with the message, not in a side channel, so they are there when you scroll back to that reply and there when a second window opens the same chat. A skill record holds what trigger matching chose, what Jev chose, whether the two agreed, the probability Jev reported and a few counts about the menu it was given. A model record holds the difficulty level, the model that answered, the model that would have answered otherwise, the probability and how long the decision took. Neither holds your message or the skill descriptions.

Each line carries a thumbs pair, and you can record whether a choice was right. The verdict is `right` or `wrong`, and it names which of the two answers you are judging -- Jev's or the normal trigger-matching one. Sending it again with a different verdict records the change of mind; sending it with the verdict spelled out as `null` takes your earlier one back. Leaving the field out altogether is refused instead, so a request that lost it does not read as taking a verdict back. Each of these appends one row to the day-file described below and never edits a row already there, so the log reads as a history rather than a current opinion. If the day-file is full the verdict is refused rather than quietly dropped, so a recorded verdict means a written one.

The thumbs on each strip line are that button, and the thumbs on the risk badge below are the same one. Either way it is an owner-only request (`POST /api/decisions/feedback`), refused for anyone but the dashboard owner, for the same reason the Decisions switch is. The verdicts land in the same daily JSONL files as the decisions, so counting them is a `jq` job over `~/.kiro/crew/decisions/*.jsonl`.

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

## Letting Jev choose steer or queue

When you send a message while the assistant is still working, it can go two ways. **Steer** interrupts the work in progress with your text. **Queue** lets the work finish and runs your message after it. The split send button has always made you choose, and the choice is a guess about a reply you have not finished reading: a "and afterwards, bump the version" sent as a steer cuts the work in half, and a "stop, wrong file" sent as a queue arrives after the damage.

With the Decisions switch on, that button offers a third mode, **Auto (Jev)**. Pick it and Jev makes that one choice for you, per message. Steer and Queue still do exactly what they did -- picking either of them asks nothing and sends nothing extra.

The mode only appears while the switch is on, your fleet permits the feature, and a turn is actually running — a session that is busy only because background sub-agents are still working has no turn to interrupt, so there is nothing to decide. It is per session, like the Steer and Queue choice already is, and if consent is later withdrawn the button goes back to Steer on its own. ⌘↩ (Ctrl+Enter) still takes the other action for one message, which from this mode is a plain queue that asks nothing.

| What you pick | What happens |
|---|---|
| Steer | Interrupts the work in progress. Nothing is sent to Jev |
| Queue | Runs after the work in progress. Nothing is sent to Jev |
| Auto (Jev), valid answer | Jev's choice of the two |
| Auto (Jev), timeout, refusal or invalid answer | Steer, the button's own default |

Auto applies only while a turn is actually running, and only to messages you send yourself: an app, an integration or a scheduled job is never decided for. Its request carries the message you just typed. It also carries a short extract of the turn in progress -- what you asked it and the newest thing it printed -- but only as far as the same `history_budget_chars` ceiling above allows, so with no consented ceiling your new message is all that leaves the machine. Anything that looks like a credential or a data-collecting URL is removed from that extract first.

The decision appears on your own message in the transcript: one line saying what Jev chose, how sure it was and how long it took, with the same thumbs you can use on a skill decision. It says the CHOICE rather than what then happened, because the two can differ — a chosen interruption cannot always be delivered, and the message then runs after the work in progress like a queued one. A message nobody decided for shows nothing.

## Measuring what a compaction should keep

When a conversation fills its context window, Kiro Crew compacts it automatically. What survives is the conversation text: your messages and the assistant's replies. Every tool call and every tool result is dropped, and that is where most of the conversation was — in a measured sample of real sessions, tool calls and their output are about seven eighths of everything the window held.

Jev can be asked, at each of those automatic compactions, which of those tool calls were worth keeping. **Nothing is kept.** The compaction happens exactly as it does today whatever Jev answers, and the answer appears as one line on the compaction notice in the chat: *Jev would keep 23 of 61 tool calls · 41% of the characters*, with a thumbs pair beside it. The word is "would". This is a measurement, so that a later version of Kiro Crew can be argued about with numbers instead of guesses.

This is off until you turn it on, and it needs a switch of its own — **Also send the conversation and tool-call inputs so Jev can score compaction**, under the tool-argument one. Turning on the main switch does not turn this on, and neither does turning on the tool-argument switch: that one was about the arguments of the single call about to run, and this is about everything the session has run, in a request one to two orders of magnitude larger. An owner who granted either of the others has not granted this.

Manual `/compact` is never measured. If you typed the command yourself, nothing is sent.

Nor is a compaction Kiro Crew did not carry out itself. Some backends shrink their own context and just tell Kiro Crew afterwards — Claude Code does it inside the reply, and KAS summarizes on its own schedule. There is nothing for this to measure on those, because the compacting is not happening here. What is measured is the automatic compaction Kiro Crew runs when a session crosses the threshold, including the restart it falls back to when that compaction does not work.

| Your session | What you see |
|---|---|
| This switch is off | Nothing — no transcript is sent and no line appears |
| You ran `/compact` yourself | Nothing — the manual command is never scored |
| An automatic compaction Kiro Crew ran, and Jev answered | One line on the compaction notice, with thumbs |
| A backend that compacts itself and reports it (Claude Code, KAS) | Nothing — Kiro Crew is not the one compacting |
| An automatic compaction with no tool calls in it | Nothing — there is nothing to score |
| Jev was too slow, refused, or answered only part of it | Nothing — the compaction notice looks as it always did |

### What leaves the machine

The conversation text of that session, and the INPUT of each tool call in it — the command, the path, the arguments. **Tool output is never sent.** Each result is replaced by how many characters it was, so a question can ask whether the output still matters without the output leaving. Passwords, keys and data-collecting URLs in the inputs are replaced with a placeholder before anything is sent.

A whole transcript is far larger than one question can carry, so it is cut down in steps until it fits: tool inputs to 1000 characters, then 200, then 60, then your messages to half their length, then to a quarter with each call on one line. The mildest step that fits is the one used. About one session in twelve does not fit even at the last step; that one is recorded as too large and nothing is sent for it.

If your organisation ships its own list of things that count as a secret, that list is applied here too, not just the one Kiro Crew ships. On a machine where that list cannot be loaded, the field is dropped rather than sent with the shorter list.

Your private reasoning is not part of this. Neither is any other session: one compaction sends one conversation.

### Waiting time, and what it costs

Nothing. The scoring runs beside the compaction, not in front of it: the compaction never waits for Jev and never changes because of it. If the scoring is slower than the compaction it is dropped and no line appears. A session with many tool calls needs several requests, and the whole run is capped at ten seconds however slow the provider is.

### What lands in the log

One row per request, as for every other decision, plus one row per compaction carrying the counts: how many tool calls there were, how many were kept whole, how many kept without their result, how many dropped, how many were pinned and never asked about (the first message and the six newest), how many were past the limit and never looked at, and three character figures — everything the transcript held, what today's compaction is eligible to keep, and what Jev's answer would have kept. That last comparison is the whole point of the exercise.

The two "eligible" figures are upper bounds, and the line says "up to" for the same reason. They count the messages a restart is allowed to bring back; the restart also has its own limits on how many messages and how many characters it will carry, so a long conversation comes back with less than these numbers say.

A compaction that could not be measured is recorded too, with its reason: the state did not fit, the run ran out of time, or only some of the batches answered. A partial answer is never shown in the chat, because "23 of 61" over a count that includes calls nobody was asked about is a wrong number rather than an incomplete one. A session with more than a thousand tool calls is measured over the newest thousand — the newest, because those are the ones the assistant is still working from — and the line says how many it did not look at — "23 of 61 (+140 not scored)" — so the count beside it is not mistaken for the whole session.

A measurement that finishes after its own compaction's notice has already been drawn is recorded and then dropped, rather than shown on the next compaction's notice.

## Choosing which recalled memories come back

When the assistant asks its own memory a question -- the `memory_recall` tool -- Kiro Crew looks through what it remembered from earlier conversations and hands back the closest matches. "Closest" means the wording is similar. That is a useful first pass and a poor last one: a note that happens to share your words gets in whether or not it helps with what you are doing, and it takes up room the rest of the prompt could have used.

With the Decisions switch on AND the recalled-memory switch under it on, Jev looks at that shortlist and says which entries to keep. Both are needed: until the second one is on, nothing about your memories is sent and every close match goes into the prompt as before. It only ever REMOVES: it cannot add a memory that was not on the shortlist, and it cannot change their order. Everything else about memory stays the same -- what gets remembered, what gets forgotten, and what `memory_recall` finds when the assistant asks for it by hand.

| State | What the tool hands back |
|---|---|
| Disabled | Every close match, as today |
| Enabled, outside the sample | Every close match, as today |
| Enabled, sampled, valid answer | The ones Jev kept |
| Timeout, refusal or invalid answer | Every close match, as today |

Keeping none of them is a valid answer, not a failure: the tool then reports no remembered conversations, which is also what a question with no close matches looks like.

Jev is only ever shown the entries the tool was going to hand back anyway, and it is only ever asked about the first twenty of them. An entry it was not shown stays in -- nothing was decided about it, so nothing removes it -- and an entry that did not fit the answer's size budget cannot be let in by Jev dropping a different one.

It happens only for a recall made in a chat you have open in the dashboard. A scheduled job, a sub-agent, an app request and any conversation whose tab is closed are never decided for, because the question sends parts of your own remembered notes and because the receipt appears on a reply you are looking at -- and those have no such reply. A conversation that started in Slack or another channel counts while you have its tab open in the dashboard: you are reading it there, so the receipt reaches you.

What leaves the machine for one of these questions is the question the assistant asked its memory (up to 2000 characters) and, for each of at most twenty shortlisted entries, its id and the first 200 characters of its text. Credentials and data-collecting URLs are replaced in that text BEFORE it is shortened, so a shortened snippet cannot end in half a key. The remembered entries are already the earlier conversation, so this question sends no separate conversation history at all, whatever `history_budget_chars` says.

Waiting for Jev cannot hold up a recall for long. The request gets the same budget as the other decisions -- `timeout_ms`, 1000 milliseconds by default -- and the wait is capped at ten seconds whatever that value says. When the time is up, every close match comes back as before.

If the request fails, the receipt says so and credits the fallback rather than Jev: the line reads "kept after fallback" and the list is titled "Kept (fallback)", because every close match went in and Jev chose none of it.

The reply carries a one-line receipt: how many entries the recall found, how many Jev kept, how sure it was on average, how long it took, and the prompt characters the smaller set saved -- `memory · recalled: 6 · Jev kept: 3 (confidence 0.81, 210 ms) · saved 2.1K prompt chars`. Open it to see which entries were on the shortlist and which survived, with a thumbs pair for each side, so you can say the plain closest-match list was the better one.

## Basic logs

Operational records are JSONL day-files under the gateway's data home, in the `decisions` directory. That directory is read-only to agents working on your machine -- by name, so a link planted at that name does not stand in for it -- so a verdict in it is one you gave. They contain the point name, hashed session identifier, elapsed time, bounded answer data and error categories. They do not contain the message body, conversation history, candidate descriptions or credentials.

A model choice writes one row for the question asked and — when the chosen model could actually be used — one further row carrying the level (`tier`), the model used (`model_chosen`), the model that would have been used (`baseline_model`) and the probability. When the level came back but could not be applied, the second row carries an error word instead: `model-not-advertised` if your account cannot run that model, `no-switch-seam` if the chat backend cannot change model mid-conversation, and `switch-failed` if it refused. Those are written on purpose, so "Jev answered and nothing happened" is visible rather than silent.

A skill selection writes one row for the question asked, carrying a `turn_id` and the number of candidates, and — when a usable answer came back — one further row for the outcome, carrying both selections: `baseline` is what trigger matching would have injected, `jev` is what was injected, `agree` says whether the two sets match, `p` is the answer's probability, and `tokens_saved` estimates the skill-body characters the difference saves, divided by four. That estimate is a rough one, and a negative value means the selection cost more than trigger matching would have. `history_chars` and `truncated` say how much conversation the request carried. A refused, timed-out or unusable turn writes only the question row, with its error category, because an agreement figure needs an answer to compare against. So one selection is two rows, and a row count is not a count of decisions.

A memory decision writes one row for the question asked, carrying a `turn_id`, the number of shortlisted memories and the length of the message excerpt, and -- when a usable answer came back -- one further row for the outcome, carrying `baseline_keys` (the ids similarity shortlisted), `jev_keys` (the ids that went into the prompt), `agree`, `p` (the average chance Jev gave one memory of being worth the prompt) and `chars_saved`. The rows carry ids and counts, never the remembered text.

These are diagnostic records, not a billing report. This feature does not provide a decisions report command. Each day-file stops growing at 8 MiB (further rows that day are dropped, with one warning), and day-files older than 14 days are deleted by the next write, so the log stays a bounded number of bounded files. A missing row alone is not proof that an answer was applied.

The provider mapping is tested locally against a loopback server. A real Jev call requires your API key; local tests do not establish real service latency or account compatibility.
