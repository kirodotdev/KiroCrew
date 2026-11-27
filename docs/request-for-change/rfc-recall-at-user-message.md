---
title: Recall at the user's message, shipped only after it passes a local bar
status: draft
author: buluoray
created: 2026-11-27
last-audited: 2026-11-27
audited-at: 958f020e279
doc-pr: null
implementation-prs: []
tracking-issues: [43083]
supersedes: []
superseded-by: []
---

# RFC: Recall at the user's message, shipped only after it passes a local bar

- Status: draft. Nothing here is built. Code is cited by symbol and file at `958f020e279`.
- Tracking issue: [#43083](https://github.com/kirodotdev/KiroCrew/issues/43083).
- Builds on: the memory index ([#42927](https://github.com/kirodotdev/KiroCrew/pull/42927),
  open; [rfc-memory-index.md](rfc-memory-index.md)), the per-message lessons block that is on
  main, and the recall usage table of [rfc-recall-reinforces.md](rfc-recall-reinforces.md).
- Evidence: every number below comes from one developer host (one bot, one owner, 56 days of
  history, 2026-10-01 to 11-26). The benchmark data is that bot's own history and stays on that
  host; only aggregate counts appear here. Each benchmark table was checked by a second model
  before it was used.
- Scope: one trigger (the user's message), evaluated locally before anything ships. Recall at task
  start or on a blocker, a post-turn judge, graph hops and an A/B test are out of scope (see
  Non-goals).

## Summary

Agents almost never search memory on their own. On the measured host, a recall tool ran on
1.4% of worker turns, 5.7% of bot-chat turns and 0% of plain-chat turns in a week. When a
mistake the owner had already corrected came back, the needed lesson was usually not in the
prompt at all, or sat in the session-start rule block dozens of turns back.

This RFC adds **one recall that runs on each follow-up message a person wrote** (not on a new
session's first message, a minimal-context turn, a temporary session, or a turn the gateway
wrote), before the agent's turn. Nothing ships until it passes a bar. **Phase 1 is local
evaluation only and is never shipped. Phase 2 ships injection, and only once one frozen setting
passes the bar on the untouched test split.**

**No tested dev setting has met the bar yet** (a lower 95% bound on precision of at least 0.6,
on at least 10 human turns). A plain cosine floor mostly injects the wrong lessons (18%
precision at the draft's floor and cap). A pre-registered grid then put a small local decision
model behind the floor (4.4.1). Its best base point was 0.50 precision (lower bound 0.24) on 9
turns; no prompt variant reached 10 turns at the bar, and lowering the floor did not help. The
deeper limit is retrieval: only 10% to 24% of the lessons the dataset marks as needed are inside
the top 10 at floors 0.55 to 0.45. This covers the tested gates (a cosine floor, and the decision
model behind it with the base prompt and nine variants), not every way to retrieve lessons.

So Phase 1 works on two things, locally (5.1): raising how many needed lessons reach the
candidates at all, and a replay test on real chats that asks whether an injected lesson makes the
answer better. Facts and experiences follow only after their missing vectors are filled in.

## 1. Motivation

### 1.1 Agents rarely recall unless asked

Counts from session transcripts, 2026-11-19 to 11-26. "Recall tool" means `memory_recall`,
`search_chat_history`, `get_chat_session`, `list_sessions`, `local_knowledge_search`,
`learn_list` or `session_ledger_read`.

| Agent kind | Sessions | User turns | Turns with a recall | Recall calls |
|---|---|---|---|---|
| Bot conductor (the bot's own chat) | 6 | 919 | 52 (5.7%) | 72 |
| Bot worker | 525 | 4,005 | 57 (1.4%) | 105 |
| Plain chat | 6 | 83 | 0 | 0 |

- 483 of 525 worker sessions never recalled anything.
- After 1,254 `work_brief` calls (a worker starting a task), a recall followed 11 times.
- A hand check of 50 turns that a regex flagged as "refers to the past" found 16 of 25 bot-chat
  turns were real references to earlier work; 8 of all 54 flagged bot-chat turns were followed
  by any recall.

All sessions ran on the KAS harness; there were no kiro-cli or Claude sessions to compare.

### 1.2 What is automatic today

| Mechanism | Where | When | What it carries |
|---|---|---|---|
| Session-start memory and lessons block | `context_assembly/store_admission.py` `read_session_memory` | Session start, and the start context re-sent after compaction | Preferences, activity, episodes ranked against the first message; lessons up to a character cap (on the measured host, 73-116 shown of 125-700 stored) |
| A bot's own facts and experiences | `context_assembly/member.py` (the "on-demand memory" essential) | Never | The prompt says "No search runs automatically" and asks the agent to call `memory_recall` |
| Per-message lessons | `context_assembly/turn_message.py` calls `store_admission.turn_lessons_block`, which calls `vector_memory_runtime/lessons.py` `turn_lessons` | Each follow-up message, only when `memory.inject_lessons_per_turn` is on (default off) | Up to 3 lessons / 2,000 chars by keyword stems with a rarity rule; `_ShownLessons` keeps one from repeating |
| `memory_recall` tool | `VectorMemoryStore.recall`; optional decision point `decisions/points/memory_recall.py` | Only when the agent calls it | Episodes and facts by cosine gate |

### 1.3 Where the lesson was when a mistake came back

62 turns where the owner corrected the same mistake again were labelled by hand; they are 38
incidents (one incident can appear as the correction and as the turn before it). One rule,
about the reply language, accounts for 41 of the turns; the owner has since set the existing
**Preferred Reply Language** setting and judges the remaining cases a trait of one model, so
that rule is a labelled subset and no headline uses it. The other 21 turns are 15 incidents
(6 rules):

| Where a needed lesson was | Incidents |
|---|---|
| In the session-start rule block (nearest needed lesson a median 57 turns back) | 10 |
| Not in the prompt at all | 5 |
| Near the request | 0 |

The block was within 10 turns on 3 of the 10, because it is re-sent after a compaction. Counted
per (incident, needed lesson): 33 of 49 were **not in the prompt**, mostly because the start
block's budget had cut them. That is the gap a per-message recall targets; this data does
not show that injecting the lesson would have prevented the repeat. Whether moving a lesson that
*is* in the prompt next to the request stops the repeat could not be measured offline either: a
replay of the original context did not reproduce the mistake, and the exact build the session
ran on cannot be reconstructed then. Phase 1 (5.1) rebuilds that test offline: a replay
of the agent turn with and without the lesson, judged blind.

### 1.4 What mature systems do

Systems that keep memory useful run a small automatic retrieval on the user's message and
keep a tool for deep search; a tool-only design depends on the model noticing that it should
look. A team running a similar recall system reported that recalling about 150 ms after the
user's message, on the real words, did most of the work for them, with a token budget and
dedupe against context
([comment on #43083](https://github.com/kirodotdev/KiroCrew/issues/43083#issuecomment-7186167138);
[SwarmAI Discussion #79](https://github.com/xg-gh-25/SwarmAI/discussions/79)). Content was
rephrased for compliance with licensing restrictions.

## 2. Goals

1. On each follow-up message a person wrote, find the lessons that answer it, and show locally
   which setting, if any, makes injecting them precise enough and makes answers better.
2. Ship nothing that has not passed that bar on the untouched test split.
3. Record which code path served each recall, so the stats describe what ran.

## 3. Non-goals

- Recall at task start, on blockers, or after the turn with a judge model. Benchmarked (4.6),
  not proposed here.
- Graph-neighbour expansion, a bucketed multi-domain recall, chat history or knowledge docs.
- A live A/B on users' traffic. The replay test in 5.1 stands in for it (5.3).
- Any shipped log-only or shadow mode.
- Changing the embedding model, or the ranking of `memory_recall`. Measured in 4.4 and left to a
  separate change.
- A reply-language check. Settled by the existing setting (1.3).

## 4. Design

### 4.1 Where it runs

In `context_assembly/turn_message.py`, in the same place and under the same guard as today's
per-message lessons block: not on a new session, not on a minimal-context turn, not when the
store's context group is excluded, never on a temporary session. It reads the same
`user_turn_text` slice, so text a dispatcher prefixed to the turn never drives the match. It
also **skips turns the gateway wrote**: worker reports, Slack report digests, automatic-recovery
notices and other system-built prompts, decided from the message's origin field, not from its
text. On dev these turns drew 24 of 62 candidate injections, 14 of them on turns that needed
nothing. When on, the
new block replaces the per-message lessons block. With the feature off, and on every timeout,
error or refusal while it is on, the prompt is exactly the feature-off prompt, including the old
per-message lessons block when `memory.inject_lessons_per_turn` is enabled.

### 4.2 Query

- Trigger 1 reuses the recall query path that is on main: since
  [#43174](https://github.com/kirodotdev/KiroCrew/pull/43174) a recall query is embedded with
  Qwen3-Embedding's query instruction (`embeddings_model.query_instruction_text`, called from `vector_memory_runtime/embedding.py`), on the query side
  only. On this trigger's own turns the instruction moved AUC from 0.636 to 0.675 (+0.040,
  interval +0.007 to +0.071) and average precision from 0.495 to 0.541 (4.4).
- The user's own text, cut to 64 tokens. 95% of bot-chat user messages are 250 characters or
  shorter, median 38. The benchmark used the full message, so the cut is checked in Phase 1
  (selections with and without it on the same dev turns).
- One query embedding per turn. Candidates: lessons in the session's bound store; the memory
  index (#42927) when it is on, today's scan otherwise.

### 4.3 Selection

1. **The gate is chosen in Phase 1, not here.** Candidates are scored by raw cosine in [-1, 1]
   (never a rescaled value). Two gates are compared side by side: an absolute floor (main's
   instructed floors, `memory_v2.INSTRUCTED_SHORT_COSINE_FLOOR` 0.55 and
   `INSTRUCTED_LONG_COSINE_FLOOR` 0.57, as the starting point) and a **relative** gate, the
   candidate's distance above the turn's own whole-store score distribution in standard
   deviations. On dev the two raise precision about equally for the same number of slots, and
   neither reaches the bar (4.4). The shipped gate and its threshold, if any, come from Phase
   1's frozen setting (5.1). Facts and experiences use production `rank_score`, a separate score space,
   and are not part of this RFC's injection (4.5).
2. **Top 1** by default, raised only if Phase 1 shows the second slot is also precise.
3. **Out-of-scope and retired lessons.** Forgotten lessons are never candidates. Lessons on a rule
   the owner has settled elsewhere (here, reply language) are logged but counted apart in
   precision.
4. **Collapse near-copies.** Lessons joined by an explicit "replaces / refines / repeats" link,
   or at cosine >= 0.90 of the same kind, count as one; the newest one not overruled is kept. An
   overruled lesson or a fragment row (a bare branch name, one word, a lone value) is never
   injected. (On dev this changed nothing; it is kept as a guard.)
5. **Dedupe against what is still in context.** `_ShownLessons` becomes a record keyed by
   memory id and revision. It describes what the context still carries: a compaction replaces
   it with what the re-sent start block carries, so a lesson compacted away can come back. A
   memory edited since it was shown counts as new.
6. **Cap:** 1,200 characters per turn.
7. **Deadline:** 250 ms of wall clock for embedding plus lookup, a proposal. Two measurements on
   the same busy host disagree: the embedding forward pass alone took 94-120 ms for 64 tokens
   (llama-bench, 4-8 threads), while the benchmark runtime took 335-365 ms p50 (733 ms p95) per
   64-token query end to end. Phase 1 times the frozen setting end to end on the same host. If its
   p95 is over 250 ms, the options are a
   shorter query cut, more embedding threads, or BGE-M3 (132 ms p50 here) as a separate change.
   On timeout, error or refusal, nothing is injected and the prompt is byte-identical to the
   feature-off prompt (4.1).

### 4.4 Benchmark (dev split only)

**License rule:** only models under a permissive open license (MIT, Apache-2.0, BSD class) are
candidates; models under custom or paid terms appear only as reference rows.

A dataset built from the measured bot's own history: corrections the owner typed, the turns
before them, worker task starts and refused commands, Slack repeat reports, and cases where
nothing should be injected. 951 dev pairs / 281 turns, 686 test pairs and 104 fact / experience
test pairs, both test files frozen with their sha256 before any tuning. Labels: three
independent audits at 90-98.6% agreement (Cohen's kappa 0.80-0.97). **No test number exists
yet**; test is read once, after every choice below is frozen, and is reported three ways
(a slice with no rule seen in dev as the headline, the fact / experience set, and the full
test labelled "includes rules seen in dev").

Embedding models on the dev pairs, cutoff-free, **excluding the reply-language rule** (798 pairs;
the rule is reported apart and never in a headline). Full message text; latency is one query on 8
threads of a busy host.

| Model | License | AUC | Average precision | Precision of top 100 | User-message turns AUC (356 pairs) | Query p50 | Peak RSS |
|---|---|---|---|---|---|---|---|
| BGE-M3 (Q8) | MIT | 0.788 | 0.695 | 0.78 | 0.752 | 132 ms at 64 tokens | 1,176 MiB |
| Qwen3-Embedding 0.6B (bundled), query instruction: on main since #43174 | Apache-2.0 | 0.704 | 0.623 | 0.76 | 0.675 | 365 ms at 64 tokens | 1,242 MiB |
| Qwen3-Embedding 0.6B (bundled), plain query: before #43174 | Apache-2.0 | 0.593 | 0.475 | 0.50 | 0.636 | 335 ms at 64 tokens | 1,231 MiB |
| EmbeddingGemma 300M (Q8), reference only | Gemma Terms (not permitted) | 0.788 | 0.738 | 0.93 | 0.780 | 59 ms at 63 tokens | 845 MiB |

With the reply-language rule included (878 pairs), AUC is 0.754 (BGE-M3), 0.664 (Qwen with
instruction), 0.590 (Qwen plain). This RFC keeps the bundled Qwen3-Embedding (Apache-2.0) with
the instruction main already uses. The permitted alternative is BGE-M3 (MIT), as a separate
change with a re-embed plan; EmbeddingGemma is not a candidate under the license rule.

**That table ranks hand-picked candidates; it does not say what the trigger would pull out of a
store.** A second benchmark runs most of the trigger's pipeline against the whole store: each of
110 dev user-message turns outside the reply-language rule, against all 1,380 lessons that
existed before the turn, with forgotten lessons excluded, near-copies merged, a per-session
dedupe window, the 1,200-character budget and the cap. Every (turn, lesson) pair any gate
variant would inject, 184 in all, was labelled by two models that saw only the message and the
lesson (agreement 0.94, Cohen's kappa 0.83). Precision below counts a slot as needed only when
both say so, with a Wilson 95% interval:

| Gate | Turns with an injection (of 110) | Slots | Precision (95% interval) | Turns helped |
|---|---|---|---|---|
| Floor 0.55, top 3 | 51 | 125 | 0.18 (0.13-0.26) | 15 |
| Floor 0.55, top 1 | 51 | 51 | 0.27 (0.17-0.41) | 14 |
| Floor 0.65, top 3 | 13 | 23 | 0.43 (0.26-0.63) | 5 |
| Floor 0.70, top 3 | 6 | 8 | 0.62 (0.31-0.86) | 4 |
| Relative gate z >= 4.0, top 3 | 14 | 17 | 0.41 (0.22-0.64) | 6 |
| Skip gateway turns, z >= 3.5 and floor 0.55, top 1 | 16 | 16 | 0.44 (0.23-0.67) | 7 |

What moves precision is selectivity (a higher floor or relative gate, about equally for the
same number of slots) and taking the top 1 instead of the top 3. Skipping gateway-written turns
does not raise it; it stays a rule because those turns are not a person's request. **No gate
reaches 0.6 with a usable firing rate**: the only 0.6 point estimate rests on 8 slots and 4
turns. The deeper limit is recall: the needed lesson clears floor 0.55 on only 14 of the 46 turns
that had one, and its median rank among all lessons is 11. This is a diagnostic, not the full
pipeline (no store-wide overrule edges, no start-block dedupe), on one bot's history, so it sets
expectations for Phase 1 rather than the shipped gate.

#### 4.4.1 A decision model behind the floor (dev, pre-registered)

The same 110 turns were run through floor -> top K -> one batched call to the decision model
(4.6) -> keep candidates above a threshold -> never re-show a lesson in a session. The design,
the selection rule and the stop rule were frozen and reviewed before any score was read. Two
blind model labellers of the same model family, who saw only the message and the lesson, judged
every top-15 candidate (and, for lower floors, every top-10 candidate on human turns), with a
re-read sample to check for drift between label batches (drift stayed under the 10-point gate). No person labelled a
sample of these pairs.
Selection counts only messages a person wrote: 85 turns, 38 of them with a candidate at floor
0.55. A setting is **eligible** when the lower 95% bound of its precision (turn bootstrap) is at
least 0.6 and its needed lessons reach at least 10 of those turns.

Stage 1 tried, at floor 0.55, K 3 / 5 / 10, thresholds 0.5 / 0.7 / 0.8, and nine variants that each
change the prompt (one change, or two in a few cells):
the question asked, the context given, the full or a short lesson text, pointwise / listwise /
pairwise shape, a yes-no or a 0-9 answer, and a few fixed examples. Stage 2 lowered the floor to
0.50 and 0.45 for the base prompt. Best points, human turns:

| Setting | Precision | Lower bound | Needed delivered / injected | Turns |
|---|---|---|---|---|
| Base prompt, floor 0.55, K 10, threshold 0.5 | 0.50 | 0.24 | 16 / 32 | 9 |
| Base prompt, floor 0.45, K 10, threshold 0.5 | 0.31 | 0.14 | 17 / 55 | 9 |
| Listwise prompt, floor 0.55, K 3, threshold 0.5 (diagnostic) | 0.88 | 0.57 | 7 / 8 | 7 |
| Cosine only, floor 0.45, top 3 | 0.21 | 0.12 | 22 / 106 | 16 |

**No setting is eligible**, including for either labeller alone or for either one saying
"needed". The listwise row rests on 8 slots and was singled out after the stage-1 scores were
read, so it is a lead for a new design, not a result. Lowering the
floor adds candidates, but the decision model does not keep the new needed ones while staying
precise. Retrieval caps everything: the share of dataset-needed lessons inside the top 10 on
human turns is 10% at floor 0.55, 20% at 0.50 and 24% at 0.45. The test split was not opened,
because no setting was picked for it.

### 4.5 Facts and experiences

In the measured store only 34.7% of facts and 31.7% of experiences carry a vector (lessons 97%),
so cosine cannot see most of them, and the keyword leg is blind for messages written in Chinese
against an English store (outside the reply-language rule, 51 of 131 needed lessons for
Chinese messages share no word with the message). New consolidation rows are now embedded at the next repair visit
([#43139](https://github.com/kirodotdev/KiroCrew/pull/43139), merged); the backlog (about 9,600
rows here, about 50 CPU-minutes estimated) is backfilled once, in batches, at low priority.
Facts and experiences enter the block only after the backfill, with their own floor (start:
production `rank_score` >= 0.6, where 11 of 15 dev results were relevant against 7 of 100 below
0.3), and carry their source session key so the agent can open the original turns.

### 4.6 What a decision model adds (not used to inject)

A small local decision model (Strands Decider v2 E2B, first-party; weights under Apache-2.0,
base model Gemma 4 E2B also Apache-2.0), on llama.cpp with Q8 weights and Q4 token tables,
re-ranked the same dev candidates in one call per turn. Excluding the reply-language rule: AUC 0.845 /
average precision 0.80 against 0.788 / 0.695 for BGE-M3. That lead comes only from task starts
and refused commands, whose labels follow a rule the model can read in the text; outside them
the difference is -0.011 (interval -0.065 to +0.040), and on user-message turns -0.009 (-0.082
to +0.061). An advantage on this trigger is **not shown** (and not ruled out). The judge
saw shorter text than the embeddings (state cut to 700 characters, memories to 300), so this is
not a model-only comparison. Cost on a busy host, one call per turn: 0.81 s p50 with one
question per candidate at 8 threads (30 turns), 1.74 s p50 with three. Behind the whole-store
floor it did not reach the bar either (4.4.1). So no judge sits on the reply path. It stays a Phase 1
candidate stage, behind a better retrieval step.

### 4.7 Served path and usage

Each recall records the code path that served it (`index_vector`, `index_words`,
`index_hybrid`, `scan_vector`, `keyword_lessons`, or `provider:<id>`), written by the serving
function, not the caller. A delivered injection (the block reached the model turn it was built
for) is counted in the recall usage table; a candidate dropped by floor, dedupe, cap, timeout or
a cancelled turn is not. Dashboard reads never count. Phase 1's local runs never count as
activations and never feed memory strength.

## 5. Rollout

One setting, `memory.recall_on_message`: `off` (default, today's behaviour) and `on`. There is no
shipped shadow or log-only mode.

### 5.1 Phase 1: local evaluation (never shipped)

Everything in this phase runs on a developer's own machine, on that owner's own history, and
ships nothing. Each study has a design frozen and reviewed before any score is read, as 4.4.1
did. Models are limited to permissive licenses (MIT, Apache-2.0, BSD class).

**(a) Raise the retrieval ceiling first.** Today only 10% to 24% of needed lessons reach the top
10, so no gate after retrieval can deliver the rest. Candidates, each measured as the share of
dataset-needed lessons inside the top K (K 5 / 10 / 20) on the dev turns, against the current
instructed-embedding baseline:

| Candidate | What it adds |
|---|---|
| Hybrid keyword + embedding | A BM25 score over lesson text fused with cosine (rank fusion), for exact names, commands and identifiers that embeddings miss |
| Query from the recent turns | The query built from the message plus the last turns (or a short rewrite of them), for messages like "do it again" that carry no topic |
| Lesson-side keys | Short keys or a one-line summary per lesson, embedded beside the full text, so a long lesson matches the situation it is about |

The winning retrieval, if any, then goes back through 4.4.1's gate step (floor, top K, the
decision model, threshold) on new blind labels.

**(b) A replay test on real chats.** For dev turns from the owner's own chats, the agent turn is
re-run twice on the same model and context: once as it was, once with the selected lessons
injected next to the message. Two blind judges see the message and the two answers in random
order (not which one had the lessons) and say which is better, or whether either repeats the
known mistake. Reported: the win rate of the injected answer with a turn-clustered 95% interval,
ties and the rate of repeated mistakes, per setting.

**The bar to leave Phase 1**, all on the untouched test split, read once after the setting is
frozen: precision lower 95% bound >= 0.6 with needed lessons on at least 10 human turns (as in
4.4.1); a replay win rate whose lower 95% bound is above 0.5; end-to-end p95 <= 250 ms; would-add
tokens per turn p95 <= 400. Any setting tried on dev after the freeze is exploration and needs its
own frozen design and test read. **If no setting passes, nothing ships**, and the report says so
with the best numbers.

### 5.2 Phase 2: `on` for lessons

Ships the frozen setting that passed 5.1, with the block injected. Its test-split numbers are the
claim. After it ships, the correction rate per 100 owner turns is reported against the months
before; that is a before / after comparison, not a controlled test.

### 5.3 What Phase 1 cannot settle

The replay test (5.1 b) judges answers on old turns; it cannot show that a user's later mistakes
become rarer. A live controlled test would: outside the reply-language rule the repeat rate is
0.79% of human turns (15 in 1,897), and seeing a 50% drop would need about 14,200-14,800
opportunities per arm with the measured clustering, which one host cannot reach. That test is a
separate proposal.

## 6. Backward compatibility

`memory.recall_on_message` defaults to `off`: prompts and stored data are unchanged. `on`
replaces the per-message lessons block (`memory.inject_lessons_per_turn`) for that session;
with `on` unset, the old toggle keeps working as today. Phase 1 adds nothing to the product.

## 7. Security considerations

- Recalled text enters the prompt as evidence, not instructions, scrubbed the way per-message
  lessons are today (`context_assembly/markers.py` `_scrub_turn_lesson`), and framed with the
  same "stored inferences do not override the current user" header.
- Only the session's bound store is read, under the same context-group and member gates as the
  start block; a bot never sees another bot's store.
- Phase 1 runs locally on the owner's own data and ships nothing. No new listener, route or
  network call.

## 8. Alternatives considered

| Alternative | Why not first |
|---|---|
| Keep recall tool-only (today) | Section 1.1: the model does not notice it should look |
| A larger session-start budget | The start block is ranked against the first message and cannot follow the topic; it is already at its cap |
| A judge model on the reply path | 0.81-1.74 s per turn on a busy CPU, and behind the whole-store floor no setting met the bar (4.4.1, 4.6) |
| Recall on every turn with a plain floor | Against the whole store, floor 0.55 with top 3 is 18% precise (4.4); production `memory_recall` admitted every returned fact in 61 real calls and 19% answered the query |

## 9. Open questions

1. Should Crew move from Qwen3-Embedding to BGE-M3 (MIT)? 4.4 says it ranks better and runs
   faster; it needs a re-embed plan, so it is a separate proposal.
2. Which replay judge is good enough? Phase 1 checks the two judges against a hand-labelled
   sample of answer pairs before it trusts them.
