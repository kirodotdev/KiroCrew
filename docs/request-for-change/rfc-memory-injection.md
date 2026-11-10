---
title: Memory lifecycle for the session-start memory block
status: draft
author: iamwhatever
created: 2026-11-09
last-audited: 2026-11-09
audited-at: 79bff1f741
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Memory lifecycle for the session-start memory block

- Status: draft
- Author: iamwhatever
- Created: 2026-11-09
- Code is cited by symbol and file at `79bff1f741`; the docs lint keeps line
  numbers out of prose, because a symbol survives the refactor that moves a line. Store numbers come from one
  developer host on 2026-11-09: the default store and one private member store,
  copied with the SQLite backup API from read-only connections and replayed
  through the shipped renderer on the copies. Only aggregate counts appear here.
- Related: [rfc-memory-hygiene.md](rfc-memory-hygiene.md) (semantic facts that
  consolidation writes, and a sweep for them) and
  [rfc-consolidation-fresh-conversation.md](rfc-consolidation-fresh-conversation.md)
  (each consolidation job on an empty conversation). This RFC covers lessons and
  the blocks that inject memory at session start.

## Summary

**Problem.** Every session starts with a rule block filled to its 37,000-character
cap, because the eligible rules are 4.4 to 4.7 times the cap. What
survives the cut is an accident of ranking. On a greeting, the default store
shows 1 of its 93 user-taught rules. Short words such as "a" admit
findings that have nothing to do with the request. Nothing ever removes a row,
so each week the pool is larger and the accident is worse.

**Cause.** Memory has four jobs: admit a row, tell whether two rows are the same
item, select rows for a prompt, and maintain the pool. Today each write path and
each block does these jobs its own way. No stage knows who taught a row, no
stage shares a notion of "same item" or "relevant", and the maintain stage does
not exist.

**Design.** One lifecycle, **Admit -> Identify -> Select -> Maintain**, built from
six shared primitives:

| Primitive | One line |
|---|---|
| P1 Value test | keep a row only if it is useful later **and** cannot be read back elsewhere |
| P2 Authority | user-taught > user-quoted > agent-inferred; decides tier, order and what may change automatically |
| P3 Item identity | one `same_item` function: normalized text, scope, tier, leader cosine >= 0.95 on stored vectors |
| P4 Relevance matcher | one token rule (>= 3 characters, CJK pairs) plus the V2 evidence gate, for every block |
| P5 Budget model | each block declares cap, gate, order and overflow line in one table |
| P6 Rollout contract | injection receipts with caps; `off -> shadow -> enforce` per stage; rollback is flag off; only Maintain rewrites rows |

Every change in this RFC is one primitive applied at one stage. Default and
private stores run the same functions.

**Rollout.** Six flagged stages (R1 to R6), each default off and shadow first,
after a flagless receipt stage (R0). Only R1 is signed so far. The first
real change orders the rule block by authority, which lifts user-taught rules
on a greeting from 1 of 93 to 81 of 93 on the default store, and to all 53 on the
private store, with no row rewritten.
The shared matcher trims findings and facts on short messages. The rule block
stays full until the pool shrinks below the cap, and only Dreaming shrinks it.
Dreaming ships last and only proposes changes for approval.

**Asked of reviewers.** Sign the framework and the stage order (sections 4 and
6), and answer the open questions in section 9.

## Terms

| Term | Meaning |
|---|---|
| Default store | the host's main memory database |
| Private store | a V2 member database owned by one crew member |
| Lesson | a row the agent should follow or know; stored with an optional `applies` tier |
| Rule tier | lessons with `applies: always` **or no tier at all** (untagged); always considered for the rule block |
| Findings tier | lessons with `applies: on_topic`; injected only when the request touches them |
| User-taught | a lesson saved on purpose: `learn_add`, dashboard edit, onboarding import (`source = user_explicit`) |
| Consolidation | the background job that turns past chats into facts and lessons |
| Rule block | the `[Learned corrections]` part of the session-start prompt |
| Injection receipt | a JSONL line recording which row ids a session-start build rendered |
| Leader fold | near-duplicate grouping where each row is compared only with rows already kept as group leaders, so chains cannot form |
| Shadow | the new policy runs and is logged; the old policy still decides what the user sees |

## 1. User problem

Users report three symptoms. Each has a number.

| Symptom | What the user sees | Measure (default / private store) |
|---|---|---|
| S1. Simple tasks cost too much | a one-word message pays for a full memory prompt | rule block on a greeting: 36,750 / 36,728 characters, at the 37,000 cap |
| S2. The agent ignores rules the user taught | a rule saved with `learn_add` is not followed | user-taught rules in the rule block on a greeting: 1 of 93 / 23 of 53 |
| S3. Memory gets noisier over time | more rows, more near-copies, more stale work state | lessons: 765 / 574 live rows; facts: 14,134 / 3,743 live rows, of which 4,551 / 2,188 have a PR, issue or round number in the key |

Supporting numbers from the same replay:

| Measure | Default | Private |
|---|---|---|
| Rule-tier rows in scope | 470 | 480 |
| Rule block uncapped (all rule-tier rows rendered) | 172,873 chars | 162,683 chars |
| Rows the capped block keeps on a greeting | 98 of 470 | 100 of 480 |
| Findings that the one-letter word "a" overlaps | 196 of 268 | 52 of 82 |
| Rule-tier rows that fold into a leader at cosine >= 0.95 | 0 | 92 |
| Injection receipt directory | 10 files, 348,666 bytes, no cap | same directory |

Counts are rows in scope for a session with no project; repo-scoped rows are
withheld before the cut. User-taught rows in the block are found by text match,
which finds every in-scope user-taught rule in the uncapped block.

## 2. Current state

### 2.1 Flow

```text
          WRITE (Admit)                      STORE                 SELECT (session start)
learn_add / dashboard ─┐                                      ┌─ rule block    (37,000, no gate)
  write_lesson 0.85 ───┼─> default store: semantic_memory ────┼─ findings      (5%, any shared word)
default consolidation ─┘   (lessons, facts, prefs)            ├─ preferences   (12,700, no gate)
                                                              ├─ task facts V1 (7.7%, no floor)
private consolidation ───> private store: memory_items ───────┼─ task facts V2 (7.7%, evidence gate)
  key = hash(rule, scope)   (directive, fact, episode)        └─ episodic      (3,000, cosine floor)
  vector filled later by backfill                              RECALL: memory_recall (hybrid)
                                MAINTAIN: none for lessons or facts
```

### 2.2 Write path (Admit and Identify today)

| Writer | Store | Tier decided by | Same-item check | Cite |
|---|---|---|---|---|
| `learn_add` and dashboard | both | the caller's `applies`; source `user_explicit` | cosine > 0.85, same scope, authority-aware | `api_lessons_create` (`dashboard/handlers/cron_lessons.py`) -> `write_lesson` (`vector_memory_runtime/lesson_write.py`) |
| Consolidation | default | model output; unstated stays untagged | same as above (`write_lesson`) | `_save_lessons` (`history_consolidation_memory.py`) |
| Consolidation | private | model output; unstated stays untagged | exact key `_lesson_key(rule, scope)`; no vector at write time | `apply_consolidation` (`vector_memory_member.py`) |
| Consolidation facts | both | kind routing, `off` by default | per key | `route_semantic_by_kind` (`memory_kind_routing.py`), `kind_routing` field (`config/memory_sections.py`) |

Write-side facts that matter for later sections:

- The tier prompt tells the model to omit `applies` when unsure, and an omitted
  tier is served as a standing rule (`LESSON_APPLIES_INSTRUCTION`, `lesson_validation.py`).
- The prompt's lesson examples are "always Y" and "never Z"
  (the lessons key of the prompt in `history_consolidation_pass.py`).
- One job may add up to 10 lessons (`_MAX_LESSONS_PER_CONSOLIDATION`, `vector_memory_constants.py`).
- Private consolidation writes no vector inside its transaction. The bulk
  backfill (`backfill_missing_embeddings` and `backfill_lesson_embeddings`,
  `vector_memory_runtime/embedding_repair.py`) fills it later at `PRIORITY_BULK`
  (`embeddings_settings.py`).
- Private consolidation rows record their source session (502 of 502). Default
  ones mostly do not (91 of 663).

### 2.3 Selection at session start

| Block | How rows are picked | Cap | Gate | Cite |
|---|---|---|---|---|
| Rule block | rank by request, then authored `always` ahead of untagged, fill to cap | `_LESSONS_STARTUP_CAP = 37_000` | none | `get_lessons_context` (`vector_memory_runtime/lessons.py`); `context_assembly/budget.py` |
| Findings | same ranking | `_LESSON_EXPERIENCE_CAP` (5%) | any shared stemmed `\w+` word, no length floor | `any_lesson_overlap`, `_lesson_words` (`lessons.py`) |
| Preferences | all `pref.*` rows | `_PREFS_STARTUP_CAP = 12_700` | none | `get_preferences_context` (`vector_memory_runtime/semantic.py`) |
| Task facts, default (V1) | hybrid rank | `_SEMANTIC_MEMORY_CAP` (7.7%) | none | `get_semantic_context` (`semantic.py`) |
| Task facts, private (V2) | candidates with evidence | same | `relevance_evidence`, cosine floors 0.62 / 0.57 | `semantic_candidates_v2` (`semantic.py`); `relevance_evidence`, `SHORT_COSINE_FLOOR`, `LONG_COSINE_FLOOR` (`memory_v2.py`) |
| Episodic | vector search | `_EPISODIC_INJECT_CAP = 3_000` | cosine 0.55 (0.42 for long text) | `_EPISODIC_RELEVANCE_THRESHOLD` (`vector_memory_runtime/episodic_search.py`) |

Ranking inputs differ by store. The default store passes a query vector to the
rule block only when the activity block already ranked against the first
message (`activity_ranked` in `context_assembly/store_admission.py`). The
private store's renderer in the same file never passes one, so its rule order
is always lexical. The ranking ignores source on purpose (the
`get_lessons_context` docstring), so a
user-taught rule has no advantage over an inferred one.

### 2.4 Recall, embeddings, receipts

- `memory_recall` (`mcp_tools/learn.py`) runs `recall`
  (`vector_memory_runtime/recall.py`), hybrid when a vector is available.
- Embeddings serve write dedup, contradiction candidates (cosine 0.4 to 0.85),
  the optional startup rank, V2 fact floors, episodic floors and recall.
- A shared token rule already exists for the JSONL store:
  `_RELEVANCE_MIN_TOKEN = 3` and CJK runs split into two-character pairs
  (`_RELEVANCE_MIN_TOKEN` and `split_cjk_word`, `lesson_validation.py`). The
  vector-store paths above do not use it.
- Injection receipts (`KINDS` and `_receipt_dir`, `injection_receipts.py`) record
  rendered ids per build and have no row, byte or age cap.

### 2.5 Maintain

Nothing shrinks the lesson or fact pool. Episodic decay changes scores, not
rows. Deletion happens only when a person or an agent calls forget or edit.

## 3. Root causes

Each symptom traced to the mechanism and the implementation cause.

| # | Symptom -> mechanism -> cause | Primary? |
|---|---|---|
| C1 | S2 -> user rules lose the cut -> **Select has no authority order**: user-taught and inferred rules compete on request overlap only, and inferred rows outnumber them 4 to 1 (default) and 8 to 1 (private) | **primary for S2** |
| C2 | S1, S3 -> rule pool 4.4 to 4.7 times the cap -> **Admit defaults to standing rule**: unstated tier means `always`, examples read as rules, no budget cost to the model; 56% (default) and 82% (private) of consolidation `always` rows name a process step (one of the words PR, CI, rebase, review, worker, conductor, push, merge, override, check; a word-list count, so an upper bound) | **primary for growth** |
| C3 | S3 -> pool only grows -> **no Maintain stage**: no path removes, merges or re-tiers a row without a person | **primary for S3**, and the reason C1 gets worse each week |
| C4 | S3 -> fact rows pile up -> Admit has no value test for work state: kind routing is off by default; 32% (default) and 58% (private) of fact keys carry a PR, issue or round number | secondary |
| C5 | S3 -> near-copies take rule room -> **no shared "same item"**: default dedups at cosine > 0.85, private consolidation by exact key, and nothing at render; 92 private rule rows fold at 0.95 | secondary |
| C6 | S1 -> unrelated findings and facts admitted -> **no shared relevance matcher**: the findings gate counts any word, even "a" (196 of 268 rows); V1 facts have no floor | secondary |

Part of C2 may be agent-relayed text: a worker's "user" turns are often another
agent's instructions. Only the private store can check this, because its
consolidation rows record their source session; 572 of the default store's 663
consolidation rows record none. R3's shadow counts quote-check passes and
failures, which answers the question for both stores going forward.

Why truncation picks by accident: C2 and C3 make the pool 4.4 to 4.7 times the cap, C1
gives the cut no notion of value, and C6 means request overlap, the only signal
left, is noise for short messages.

## 4. Design: one framework

### 4.1 Lifecycle

| Stage | Question it answers | Runs when | May rewrite stored rows? |
|---|---|---|---|
| Admit | should this new row exist, and at which tier? | each write | no; decides only the new row |
| Identify | is this row the same item as one we have? | write, backfill, render, Maintain | no; it may retire only a row the current consolidation just wrote |
| Select | which rows enter this prompt, in which order? | session start, recall | no; render only |
| Maintain | which existing rows should merge, re-tier or retire? | offline (Dreaming) | yes, with approval and revisions |

### 4.2 Shared primitives

**P1 Value test.** A row is worth keeping only if it is useful in a later
session **and** cannot be read back from another source (GitHub, git, a work
ledger, the file system). Kind routing already applies this to facts (a `state`
item is skipped under `enforce`). Admit applies it to lessons through the
prompt and the P2 tier rule. Maintain applies it to the backlog.

**P2 Authority.** Every row gets one of three levels from data it already has:

| Level | Rows | Default tier | Order in rule block | Automatic change allowed |
|---|---|---|---|---|
| L1 user-taught | `source = user_explicit` | as the person set it | first | never |
| L2 user-quoted | consolidation row with a verified `user_quote` | `always` allowed | second | never; approval only |
| L3 agent-inferred | every other consolidation row, including untagged legacy rows | `on_topic` for new rows | third | render fold; Maintain proposals |

`learn_add` is called by the agent, usually because the person asked. This RFC
takes `user_explicit` at its word. Tightening that is open question Q4.

The `user_quote` check, in code: the quote, after NFKC normalization, case
folding and whitespace collapse, must be a substring of one turn whose role is
the person's and that carries no relayed-message marker. A turn the person
edited is matched in its final stored form. A quote shorter than three P4
tokens does not count. A relayed agent message never counts, even if it quotes
the person.

**P3 Item identity.** One function `same_item(a, b)` used everywhere:

1. Same scope and same tier, or never the same.
2. Same normalized text (P2's normalization) -> same.
3. Otherwise, both rows have stored vectors of the same dimension, nonzero,
   from the current embedding space, and cosine >= 0.95 -> same.
4. Any vector missing, zero, of another dimension or another space -> not the
   same. Embed failure therefore means today's behavior.

Grouping is leader-based: rows are visited in P2 order, then newest first, and
each is compared only with leaders already kept. An L1 row is always a leader
and is never folded into another row.

**P4 Relevance matcher.** One token rule for every keyword comparison:

- A Latin or digit token counts only at 3 or more characters.
- A CJK run yields its two-character pairs; a one-character run yields none.
  This is the shipped `split_cjk_word` rule, reused unchanged.
- Admission needs at least one shared significant token.
- For task facts, both stores use the V2 gate: a row must carry
  `relevance_evidence` (lexical token or cosine above the 0.62 / 0.57 floors).

**P5 Budget model.** Each block is one row of one table (section 4.3, Select).
A block declares its cap, its admission gate, its order, and the line it prints
when rows do not fit. The rule block has no relevance gate, by design: a
standing rule applies whatever the topic.

**P6 Rollout contract.**

- Every stage has a flag `off | shadow | enforce`, default `off`.
- `shadow` computes the new decision and writes it to the injection receipt; the
  old policy still decides. Receipts hold row ids and counts, never text.
- Receipt caps copy kind routing (`SAMPLES_PER_DAY` and its siblings, `memory_kind_routing.py`): 150 sampled
  rows a day, 2,000 rows total, 1 MB, 14-day sample window, counters kept 400
  days, reset on a policy-version change. New receipt kinds join `KINDS`.
- Rollback is the flag set to `off`. No stage except Maintain rewrites an
  existing row, so no rollback needs a data migration.
- A flag is retired when its `enforce` has been the default for 30 days with no
  rollback: one follow-up pull request deletes the flag, its `off` and `shadow`
  code and its receipt fields.

### 4.3 The stages, as instances of the primitives

**Admit.**

| Change | Primitive | Applies to |
|---|---|---|
| New consolidation lesson with no tier is stored `on_topic` | P2 (L3 default) | both stores |
| `applies: always` from consolidation needs a verified `user_quote`, else `on_topic` | P2 (L2 test) | both stores |
| Lesson prompt states P1; examples show one finding and one standing rule | P1 | both stores |
| Kind routing moves to `enforce` for facts | P1 | both stores; its own flag and RFC already exist |

`learn_add` and dashboard writes are unchanged. Existing rows are unchanged.

**Identify.**

| Where | What runs | Effect |
|---|---|---|
| Private consolidation, in the write transaction | P3 steps 1-2 (normalized text) | a textual copy merges into the existing row |
| Private consolidation, when backfill gives the new row its vector | P3 step 3, only for rows this consolidation wrote and that are not yet served | a match retires the new row with a revision and counts it; the leader is untouched |
| Render, rule block | P3 leader fold | duplicates take no room; the block prints "folded N near-duplicate rules; learn_list shows all" |
| Maintain | P3 plus an uncertain band | proposals only (section 7) |

The default store keeps its existing `> 0.85` newest-wins merge in
`write_lesson` for now. Shadow logs how many of its merges fall between 0.85 and
0.95, so reviewers can decide whether to move it to P3 (Q1).

**Select.** The budget table, after this RFC:

| Block | Cap | Gate | Order | Overflow line |
|---|---|---|---|---|
| Rule block | 37,000, unchanged | none | P2 band (L1, L2, L3), then request relevance inside a band, after P3 fold | existing omission line, plus the fold line |
| Findings | 5%, unchanged | P4 | relevance | existing withheld line |
| Preferences | 12,700, unchanged | none | unchanged | unchanged |
| Task facts | 7.7%, unchanged | P4 plus V2 evidence, both stores | unchanged | unchanged |
| Episodic | 3,000, unchanged | unchanged cosine floor | unchanged | unchanged |
| Activity (history, projects) | unchanged | P4 | unchanged | unchanged |

Fit of the authority order on today's stores, replayed on a greeting: the L1
rows alone render to 42,333 characters on the default store and 21,240 on the
private store. The capped block keeps 81 of 93 user-taught rules on the default
store and all 53 on the private store. When L1 alone overflows, the existing
omission line says so and points at `learn_list`.

Cost on simple tasks: P4 shrinks the findings, facts and activity blocks on short
messages at once. The rule block stays at its cap until Maintain brings the
rule pool below 37,000 characters.

**Maintain.** Dreaming (section 7). It is the only stage that changes existing
rows, and it uses P1, P2 and P3 to find candidates.

### 4.4 Where the earlier fixes went

| Earlier fix | Now | Stage and primitive |
|---|---|---|
| A. Consolidation tier default and quote | the L3 default and the L2 test | Admit, P2 |
| B. Private store write dedup at 0.95 | P3 at write and at backfill | Identify, P3 |
| C. One word-length relevance rule | the shared matcher | Select, P4 |
| D. Render-time near-duplicate fold | P3 at render | Identify at render, P3 |
| E. Dream cleanup | the Maintain stage | Maintain, P1 to P3 |
| Optional "user-taught first" | core: the authority order | Select, P2 |

### 4.5 One code path for both stores

Both stores already render through `get_lessons_context`
(both renderers in `store_admission.py`). The primitives live in one module and take a
store as input: `authority(row)`, `same_item(a, b)`, `relevant(tokens, row)` and
the block table. The only per-store difference left is where Admit runs
(`write_lesson` versus `apply_consolidation`), and both call the same Admit
function. The private store's missing startup query vector stays missing; P2
and P4 do not need it.

### 4.6 Options left out, and why

| Option | Why it is out, in framework terms |
|---|---|
| Relevance floor on the rule block (lexical or cosine) | breaks P5's rule-block row: a standing rule applies regardless of topic; a floor drops L1 rules on unrelated requests, and short queries do not separate related from unrelated rules by cosine |
| Exempt "safety" rules, then floor the rest | the exempt set alone exceeds the cap; P2 already orders what matters |
| Lower the 37,000 cap | a budget change without fixing order or pool; L1 alone renders to 42,333 characters on the default store, so a lower cap drops more user-taught rules |
| Write dedup across scopes | P3 step 1: scope is part of identity; merging across scopes breaks scope isolation |
| Single-link clustering | P3 is leader-based because chains merge different rules: at cosine 0.90 single-link builds a 50-row private cluster whose weakest pair is 0.73 |
| Identity at cosine 0.85 to 0.90 | the private fold grows from 92 rows at 0.95 to 144 at 0.90 and 185 at 0.85 with no evidence that the added pairs are the same rule; 0.95 stays until Maintain approvals measure the band |
| Greeting word list | P4 must be general; a per-language list is never complete, and the short-token rule fixes greetings and every other short match |
| Model update or delete at write time | Admit is code-checked; a model edits rows only in Maintain, behind approval |

## 5. Regression safety

### 5.1 What never changes

| Invariant | Enforced by |
|---|---|
| An L1 row is never dropped, merged, folded into another row or re-tiered automatically | P2 table; code gate in Identify and Maintain |
| Untagged legacy rows are still served as rule-tier rows | Admit changes new rows only; only Maintain proposes a re-tier, with approval |
| Embed failure means today's behavior | P3 step 4; no stage adds a query embed or a new timeout |
| No stage but Maintain rewrites an existing row | P6; Identify retires only rows the current consolidation wrote |
| Rendering never writes the store | receipts are held in memory and written after dispatch |
| Caps are unchanged | P5 table |

### 5.2 Shadow metrics and go/no-go

Each stage in shadow records what `enforce` would add, drop or fold. The miss
signals, per row the new policy would drop:

| Miss signal | Meaning |
|---|---|
| a later `memory_recall` in the same session returns the dropped row | the row was needed |
| a rule is re-taught at cosine > 0.85 to a dropped row | the user had to repeat it |
| an L1 rule is absent from a block it was in before | a hard regression |

**Control.** A fixed random sample of the rows the **current** policy omits.
Its miss rate is the cost users already pay today. A stage passes when, over at
least 14 days and 200 session starts:

- its miss rate is no higher than control, and
- the L1-absence count is zero for stages that can remove a row from a block.

### 5.3 Rollback

Set the stage's flag to `off`. Admit, Identify and Select take effect at the next
session start. Rows Identify retired at write are restored from their revisions
by the same revert path Maintain uses.

## 6. Rollout

Each stage is one pull request, independently shippable and abandonable.

**Signed scope.** A maintainer has signed R1 only (authority order, flag
`memory.rule_authority_order`, default off, shadow first). R3's quote check and
tier policy wait for shadow counts of how many would-be demotions the user
repeats. R2 and R4 to R6 are proposals; each needs its own sign-off.

| # | Stage | Flag (default `off`) | Shadow signal | Pass metric | Rollback |
|---|---|---|---|---|---|
| R0 | Receipt fields and caps | none (fields written only under a stage's `shadow`) | n/a | receipts stay under every cap in a test that writes past them; off-mode lines unchanged byte for byte | revert the PR |
| R1 | Select: authority order | `memory.rule_authority_order` | ids that enter and leave the rule block | L1 coverage up; miss rate of dropped L3 rows <= control | flag off |
| R2 | Select: shared matcher | `memory.relevance_rule` | rows admitted only under the old gate | miss rate <= control | flag off |
| R3 | Admit: tier policy | `memory.lesson_tier_policy` | new keys that would be stored `on_topic`; quote check pass and fail counts | re-teach rate of would-be `on_topic` rows <= control | flag off; rows already written keep their tier and stay retrievable through findings and recall |
| R4 | Identify: render fold | `memory.lesson_fold` | (leader, folded) id pairs | no L1 folded; miss rate <= control | flag off |
| R5 | Identify: private write and backfill | `memory.member_lesson_dedup` | would-be merges; default store 0.85-0.95 merge count | miss rate <= control; zero L1 merges | flag off; revert retired rows from revisions |
| R6 | Maintain: Dreaming | `memory.dream: off \| propose` | approval lists, refusals, wake-check results | section 7.9 | flag off; revert any run |

R1 comes first because it fixes S2 on existing stores without rewriting a row.
R4 needs one test fix first: the golden fixture that gives every topic-less
row the same vector must give distinct vectors, or it folds everything.

## 7. Advanced: Dreaming (Maintain)

> **Higher risk.** Dreaming is the only stage that changes rows that already
> exist, and the only one where a model proposes edits. Everything below is
> there to keep it reviewable, reversible and bounded. It ships last, in
> `propose` mode only.

### 7.1 Why it is needed

Admit slows growth, Identify removes copies from the prompt, and Select orders
what is left. None of them shrinks the pool. The rule pool is 172,873 and
162,683 characters against a 37,000 cap; until it shrinks toward the cap, the
cut still decides most of the block. Maintain is the only stage that can bring
the pool down. The cap does not change.

### 7.2 What others do

Summaries below are our paraphrase of each source.

| System | What it does | What we take |
|---|---|---|
| Claude Code Auto Dream ([guide](https://claudefa.st/blog/guide/mechanics/auto-dream)) | a background pass that runs only after both a day has passed and several sessions happened; one run at a time under a lock; narrow searches for corrections and saves; merge and prune; unchanged files left alone | dual gate, lock, narrow candidate search, leave unchanged rows alone |
| Letta sleep-time compute ([blog](https://www.letta.com/blog/sleep-time-compute), [paper](https://arxiv.org/abs/2504.13171)) | a separate agent works on memory while the main agent is idle; Letta also names overgeneralization and memory rot as risks of repeated rewrites ([post](https://www.letta.com/blog/towards-agents-that-learn/)) | run only when idle; treat model rewrites as a risk to measure |
| Mem0 ([migration guide](https://docs.mem0.ai/migration/platform-v2-to-v3)) | moved from write-time ADD/UPDATE/DELETE to an ADD-only write pass, with ranking at read time | no model edits at write time |
| SAGE ([arXiv 2605.30711](https://arxiv.org/abs/2605.30711)) | code settles clear additions and clear duplicates; only uncertain cases reach a model merge | code finds candidates; the model sees only the uncertain band |
| Nocturne ([write-up](https://dev.to/harishkotra/sleeptime-memory-consolidation-an-agent-that-edits-its-own-memory-and-proves-it-still-remembers-53be)) | the model proposes, a pure code gate applies; refusals logged; rows deactivated, never deleted; a recall quiz after each pass | code gate, logged refusals, tombstones, wake check |
| Anthropic dreaming ([research](https://www.anthropic.com/research/components-of-agent-memory), [cookbook](https://platform.claude.com/cookbook/managed-agents-cma-dreaming-with-a-dynamic-workflow)) | a background pass re-reads past transcripts to check what memory claims, fixes notes that were wrong when written or went stale, and dates facts so a later one supersedes an earlier one; in one reported run a wrong note sent the agent into the same trap three times until the transcript check fixed it; analyzers get file tools only and treat transcripts as data | the transcript is the ground truth a row is checked against; evidence comes from the session, never from the model; transcripts are untrusted input |

### 7.3 Trigger

All must hold:

1. At least 24 hours since the last completed run, **and** at least 20 new
   lesson rows or 5 sessions since then.
2. No chat turn is running in any session.
3. A cross-process lock file under the data home is free. One run per host.

At most one automatic run a day. A manual trigger runs the same pass and still
takes the lock and the idle check.

### 7.4 Scope of one run

- Incremental from a watermark in `memory_meta`: rows written or updated since
  the last run, plus the existing rows they are compared with.
- The first run over the existing backlog is capped per night and resumable: it
  keeps its own cursor and finishes over several nights.
- Per run: at most 200 rows considered and 10 minutes of wall clock. The run
  stops at the next batch boundary when any chat starts.
- Rows that lack a vector are embedded on the bulk queue.

### 7.5 Candidates, found by code

| Candidate | How found | Model? |
|---|---|---|
| Exact copy | P3 steps 1-2 | no |
| Near-duplicate | P3 leader fold at >= 0.95 | no |
| Uncertain band | leader cosine 0.90 to 0.95, same scope and tier | yes, to write one merged sentence |
| Work state | P1: key and value patterns kind routing tags `state` | no |
| Numbered key families | P1: keys differing only by a PR, issue or round number | no; grouped for review |
| Legacy tier | P2: L3 rule-tier rows (untagged or `always` without a quote) | no; proposed as `on_topic` |
| Transcript evidence | a row in this run's batch with a known source session, and a later session that served it and mentions its key terms (7.5.1) | yes, to judge the excerpts |

Only the uncertain band and transcript evidence reach the model: a few groups
and excerpts per run, under one cap of 20,000 input tokens.

#### 7.5.1 Transcript evidence

Every other candidate compares a row with other rows. None of them can tell
that a row was wrong when written, or that a later session showed it stale.
This candidate checks a row against the sessions it came from and was used in.

- **Source session.** Read from `memory_items.session_key`
  (`memory_schema.py`), or on a member store from the `source_ref` that
  `apply_consolidation` stamps (`vector_memory_member.py`). A row with neither
  is not a candidate. Most default-store consolidation rows carry no source
  today, so this candidate mainly reaches member stores until they do.
- **Later use.** Sessions since the watermark whose injection receipts
  (`injection_receipts.py`) list the row's id. Receipts carry the session key,
  so the transcript is read locally at check time.
- **Excerpts, not transcripts.** Code searches those transcripts for the row's
  P4 content words and keeps the matching turns plus one turn on each side. The
  model never reads a whole session. A row with no matching turn is skipped.
- **Proposals.** `confirm` (records a dated confirmation, changes nothing
  else), `correct` (carries corrected text) or `supersede` (names the newer row
  or excerpt that replaces it). Each carries the excerpt it relies on.
- **Order.** The row's own source span is checked first, later sessions second.
  A row that was right when written and later contradicted is superseded with a
  date, not marked wrong.
- **Authority.** On L1 and L2 rows only `confirm` or a flag for the person's
  review is allowed, never `correct` or `supersede` (7.6).

### 7.6 Model proposes, code gates

Each proposal passes a pure check over store state before it reaches the
approval list:

- Every cited id exists, is live, and was in this run's batch.
- No L1 or L2 row is changed.
- A merged sentence adds no content word absent from its source rows.
- A row appears in at most one proposal per run.
- A transcript-evidence excerpt must be a substring of a turn in a session this
  run cited for that row, the same check P2 applies to `user_quote`. A
  `correct` text adds no content word beyond the row and that excerpt.

A refused proposal is logged with its reason and counted in the run receipt.

### 7.7 Approve, apply, restore

- Output is an approval list, not a change. Deletions and merges need a
  person's approval.
- An approved change goes through the existing edit and forget paths. They
  write `memory_revisions` and leave a tombstone, so every row restores
  (`memory_edit.py` reads `memory_revisions` to restore a prior value).

### 7.8 Wake check

After an approved batch is applied:

1. Sample recent real startup queries from injection receipts. Receipts carry a
   session key and message id, so the query text is read from the local
   transcript at check time and never copied into a receipt.
2. Render the rule block and run recall for each query, before and after.
3. The batch fails if any L1 or L2 rule leaves the rule block, or if a recall
   hit is lost without its merged leader taking its place.
4. A failed batch reverts itself through the same revisions.

### 7.9 Resource bounds and pass metric

| Bound | Default |
|---|---|
| Automatic runs per day | 1 |
| Rows per run | 200 |
| Time per run | 10 minutes |
| Model input per run | 20,000 tokens |
| Embedding queue | bulk priority |
| Stop condition | a chat turn starts |

`propose` passes when approval lists are produced, every refusal is logged, and
the wake check passes on every applied batch for 14 days.

### 7.10 How trust may widen

One narrow class may later apply without approval, for example exact copies
among L3 rows. That needs a measured record of zero bad merges (approved, then
restored by a person or failed by the wake check) across a stated number of
approvals in that class (Q2). It is its own flag and its own decision. L1 and L2
rows never join an automatic class.

### 7.11 Failure modes and kill switch

| Failure | Guard |
|---|---|
| A merge loses meaning | code gate on content words; approval; wake check; revisions |
| A model invents a row or touches an unseen row | ids must be in the batch |
| The run slows a chat | idle check, bulk queue, stop at batch boundary |
| Two runs collide | cross-process lock |
| The backlog run never ends | per-night cap with a cursor |
| Anything else | `memory.dream: off` stops scheduling at once; any run reverts from its revisions |

## 8. Security considerations

- Every stage writes only the memory store, through existing write, edit and
  forget paths.
- Receipts hold row ids and counts, not text.
- Dreaming sends the text of uncertain-band rows and transcript-evidence
  excerpts, at most 20,000 input tokens a run, to the model provider the
  consolidation job already uses. That provider already received those
  transcripts when the sessions ran. No new destination is involved.
- Transcripts hold text from web pages, issues, channel messages and tool
  output, so excerpts reach the model as quoted data, never as instructions.
  The excerpt substring gate means a proposal cites only text really in the
  session, and the approval list means an injected excerpt can at worst propose
  a change a person declines. Incognito and temporary sessions write no
  receipts and are never read.
- The code gate refuses any id outside the batch, so model output cannot touch
  rows it was not shown.
- The `user_quote` check keeps relayed agent text from becoming a standing rule.

## 9. Non-goals and open questions

**Non-goals.**

- Lowering any cap.
- A relevance floor on the rule block.
- Write dedup across scopes.
- Automatic deletion of any row.
- Single-link clustering, or identity below cosine 0.95.
- A new shadow log beside injection receipts.
- Changing who may call `learn_add`.

**Open questions for reviewers.**

1. Q1. Should the default store's `> 0.85` write merge move to P3's 0.95? R5
   shadow data counts the merges in between.
2. Q2. What zero-bad-merge record lets Dreaming apply exact L3 copies without
   approval?
3. Q3. Are 20 new rows or 5 sessions the right Dreaming trigger for a light user?
4. Q4. `learn_add` is agent-called. Should L1 require the person's own turn to
   contain the request, as the L2 quote check does?
5. Q5. When L1 alone overflows the rule block (81 of 93 fit on the default
   store), should Maintain propose merges among L1 rows for the person to
   approve, or should L1 get its own budget line?
6. Q6. Should transcript evidence (7.5.1) share the 20,000-token cap with the
   uncertain band or get its own, and how many rows should one run check?
