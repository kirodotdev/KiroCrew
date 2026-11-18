# Worker grill

A worker grill settles one costly choice fast. Two or three sub-agents talk it through in short turns, a moderator worker runs them, and the owner only steers the direction. It ends in a brief, never in questions: facts are settled by evidence, agreed points stand, and the moderator picks each taste choice by its recommendation and says so.

It runs only while the `worker_grill` feature preview is on, which is when your turn carries the "Worker grill preview is on" paragraph. Then it replaces the skill's unasked debate offer; the formal debate in `worker-debate.md` stays for when the owner names one.

## The conductor's part

1. **Offer.** When a choice has two or more plausible paths and a wrong pick is costly, add "Let workers grill it first" to that reply's `[OPTIONS:]`. Costly means it crosses a security, data, approval or trust boundary, changes a contract others depend on, or follows a failed attempt or a reviewer's design block. Size alone is not a reason. Do not offer it for a small clear fix, CI or rebase work, wording, an emergency, a choice the owner already made, or when fewer than half the goal's work items are left.
2. **Card.** When the owner picks it, reply with one `[GRILL: {...}]` line, on its own line and before any `[OPTIONS:]` line. At most one per reply.

   ```
   [GRILL: {"q":"Who writes the welcome's facts?","must":"No wrong PR state again","skip":"The Board layout","who":[{"name":"A","stance":"For plan A"},{"name":"B","stance":"For plan B"},{"name":"C","stance":"Critic: can it go wrong again?"}],"minutes":15}]
   ```

   `q`, `must` and `skip` are at most 200 characters each. `who` has 2–3 entries; `name` is at most 20 characters and `stance` at most 80. `minutes` is an integer from 1 to 60. The line must be valid JSON; anything else shows as plain text. End the same reply with an `[OPTIONS:]` line of two choices in the owner's language, one to start the grill and one to skip it: where the dashboard draws the card, the card replaces those buttons; where it does not, the buttons still let the owner answer.
3. **Start.** The card's messages follow the owner's dashboard language, so they may be in any language; never match them by fixed words. A start from the card is one opening line followed by three labelled lines: question, must and not discussing, which carry the owner's edits and win. The owner's start choice without those lines starts the grill with the card as you wrote it. Dispatch ONE `kirocrew-worker` as moderator, one work item, with the question, must and skip, the participants and stances, the minutes, the owner's own words quoted verbatim, and this file's path. The skip choice or the card's decline, in any language, drops the grill.

   **Card key.** The card's start and decline may carry one more line, `key: <value>`. That line is a wire value, never translated: only a whole line exactly `key: <value>` counts, and the value is opaque (copy it, never parse it). For a keyed start, end the moderator item's title with `grill key:<value>`. Ignore a keyed start when any grill item in the ledger, open or closed, has a title that ends in exactly that `grill key:<value>`. A start without a key line is ignored when the same question already has an open grill item. Either way, whatever language it is in, dispatch no second moderator. A keyed decline just drops the grill. Limits: the ledger keeps only the last 32 closed items, so an older key is forgotten; two starts that arrive before you write the item can still start two moderators.
4. **Steer.** When the owner adds a direction while it runs, send it to the moderator with `session_send` and `steer: true`, word for word.
5. **Brief.** The moderator's `done` carries the conclusion in `summary` and the brief in `artifacts.brief`. Post the brief link and the one-line conclusion in the chat, then an `[OPTIONS:]` line with one choice in the owner's language meaning "Not satisfied, run another grill". Build on the conclusion like any other item result.
6. **Again.** When the owner picks that choice and names what they disliked, answer with a new `[GRILL: {...}]` card whose `q` is that point, then follow steps 2–3. The new grill is a new item, so the duplicate rule in step 3 does not stop it.

## The moderator's part

You are the moderator. You do not take a side.

1. **Thread.** Write `thread.md`: the question, must, skip, each participant's name and stance, `direction v1` (the owner's words), and the time cap. Save it with `artifact_save` and keep its link. Every round appends to it. Before round 1, send one `work_report status=progress` with the `discussion` field of step 5 (`phase` `talking`, `round` 1), so the Board shows the grill from the start.
2. **Rounds.** Each round, run all participants at once with `spawn_sub_agents`, on the same model. Give each its name, its stance, the whole `thread.md` text and this rule: answer the latest points in at most 5 sentences; a fact carries a `path:line` or the command output that shows it; say "no new point" when you have none. Append each reply under its name, then update the artifact.
3. **Deep check.** When one fact is disputed and would change the conclusion, start one `spawn_run` to check it, and keep the talk going on the other points. Paste its finding into `thread.md` when it returns. Never more than one check at a time.
4. **Direction.** A direction the conductor relays is read at the start of the next round. Add it as `direction v2` (and so on) and tell the participants.
5. **Report each round.** `work_report status=progress`, with a `discussion` field (added to `work_report` by #37340): `phase` (`talking`, `checking` or `done`), `round`, `minutes`, `direction`, `agreed` (at most 8), `disputed` (at most 5, each a point and the sides), `checking` (at most 3), `recent` (at most 6, each who and text) and `thread` (the artifact link). Send the whole object every time; the newest replaces the last. Put the agreed and disputed points in `summary` too.
6. **Stop** when two rounds in a row bring no new point, after 6 rounds, at the time cap, or when the owner says stop. The time cap is the card's minutes, 15 by default: once it is reached, start no new round; finish the round in flight, then stop.
7. **Settle.** A point every participant agreed on is decided. A fact is decided by its evidence. What is left is taste, a choice the evidence cannot make: pick the side you recommend and record why. Never ask the owner: no `status=question`, no asks.
8. **Brief.** Write the brief and save it with `artifact_save`: the conclusion; the reasons (the agreed points); each disputed point you picked, marked "moderator picked this for you", with the side and why; the risks; how to undo it. Report `done` with the conclusion in `summary`, the brief link as `artifacts.brief` and the thread link as `artifacts.thread`.

**When sub-agents are refused.** If `spawn_sub_agents` or `spawn_run` is refused in your session, play each participant yourself in turn, from its stance, within the same rules. Say so in `thread.md` and in your `done` summary: the talk is faster but less independent.
